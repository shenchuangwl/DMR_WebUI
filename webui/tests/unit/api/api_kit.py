"""控制接口测试的公共帮手。

- ``AuthRig``：只装登录与席位两个服务（真实实现 + 临时账本 + 手动时钟 + 一个字典当配置），给协程测试直接 ``await`` 用。
- ``Kit``：整个应用——真实的账本、总线、模型状态、登记簿、配置、风控闸、合约数上限、告警、三个模型，
  加进程内的 starlette TestClient（不开端口；用 ``with`` 进入，所以应用的 lifespan 跑在 ``client.portal`` 的事件循环里）。
  同步的测试代码通过 ``kit.run(协程)`` 把协程交给那个事件循环执行（登记簿、总线、事件发布都必须在那个循环里做）。

口令哈希用最小的 scrypt 参数（FAST），免得每次登录花 0.3 秒；哈希串里带参数，校验照常。
"""
from __future__ import annotations

import secrets
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from dmr_kernel.api import auth as auth_mod
from dmr_kernel.api.auth import AuthService, LoginLimiter, ScryptParams
from dmr_kernel.api.common import LoginResult, Principal, SeatChanged
from dmr_kernel.api.seat import SeatService
from dmr_kernel.core.clock import ManualClock
from dmr_kernel.core.events import EventBus
from dmr_kernel.ledger import repo_alerts, repo_audit, repo_auth, repo_orders
from dmr_kernel.ledger.records import OperatorRecord
from tests.support.ledger import START_MS

FAST = ScryptParams(n=2**4, r=1, p=1)
PASSWORD = "correct-horse-battery"
BASE = "/api/v1"
D = Decimal
T0 = START_MS


def default_config() -> dict[str, Any]:
    return {"session_idle_min": 30, "reauth_window_s": 300, "seat_lease_s": 60}


# ---------------------------------------------------------------- 交易所数据的构造帮手


def position_info(symbol: str = "BTCUSDT", side=None, qty: Decimal = D("0.5"), entry: Decimal = D("60000"),
                  mark: Decimal | None = D("61000"), pnl: Decimal | None = D("500"),
                  liq: Decimal | None = D("30000"), at: int = T0):
    from dmr_kernel.core.types import PositionInfo, PositionSide

    return PositionInfo(symbol=symbol, side=side or PositionSide.LONG, qty=qty, entry_price=entry, mark_price=mark,
                        liquidation_price=liq, unrealized_pnl=pnl, leverage=2, margin_mode="isolated",
                        update_time_ms=at)


def order_info(symbol: str = "BTCUSDT", cid: str | None = "DKA-1-1-o", oid: str = "1001", side=None,
               position_side=None, order_type: str = "LIMIT", qty: Decimal = D("1"), filled: Decimal = D("0"),
               avg: Decimal | None = None, status: str = "NEW", at: int = T0):
    from dmr_kernel.core.types import OrderInfo, OrderSide, PositionSide

    return OrderInfo(symbol=symbol, client_order_id=cid, exchange_order_id=oid, side=side or OrderSide.BUY,
                     position_side=position_side or PositionSide.LONG, order_type=order_type, qty=qty,
                     filled_qty=filled, avg_price=avg, status=status, raw_status=status, update_time_ms=at)


def algo_info(symbol: str = "BTCUSDT", cid: str | None = "DKA-1-1-s", aid: str = "2001", side=None,
              position_side=None, algo_type=None, trigger: Decimal = D("59000"), raw: str = "NEW", at: int = T0):
    from dmr_kernel.core.types import AlgoOrderInfo, AlgoType, OrderSide, PositionSide, WorkingType

    return AlgoOrderInfo(symbol=symbol, client_algo_id=cid, algo_id=aid, side=side or OrderSide.SELL,
                         position_side=position_side or PositionSide.LONG, algo_type=algo_type or AlgoType.STOP_MARKET,
                         trigger_price=trigger, working_type=WorkingType.MARK_PRICE, close_position=True, qty=None,
                         raw_status=raw, actual_order_id=None, create_time_ms=at, update_time_ms=at)


def position_record(**overrides):
    """一条完整的持仓记录（托管中的策略多仓），字段可按名覆盖。"""
    from dmr_kernel.core.types import ExitRuleset, Ownership, PositionSide, PositionStatus, ProtectionState
    from dmr_kernel.ledger.records import PositionRecord

    fields: dict[str, Any] = dict(
        id=1, symbol="BTCUSDT", side=PositionSide.LONG, generation=1, status=PositionStatus.MANAGED,
        ownership=Ownership.STRATEGY, is_mixed=False, pending_reason=None, pending_flags=[], qty=D("0.5"),
        avg_price=D("60000"), max_qty=D("0.5"), open_time_ms=T0, open_time_estimated=False, anchor_ms=T0,
        stop_cycle=0, protection_state=ProtectionState.SYSTEM, stop_price=D("58000"), stop_client_id="DKA-1-1-s",
        stop_algo_id="2001", stale_stops=[], handed_over_stop=None, stop_note="", exit_ruleset=ExitRuleset.STRATEGY,
        signal_exit=False, leverage=2, margin_mode="isolated", liquidation_price=D("30000"), exchange_qty=D("0.5"),
        exchange_seen_ms=T0, consistent=True, origin_note="", opened_by_intent_id=1, list_snapshot_id=None,
        list_direction=None, list_score=None, entry_reason="test", exit_reason=None, closed_ms=None, version=3,
        created_ms=T0, updated_ms=T0)
    fields.update(overrides)
    return PositionRecord(**fields)


def order_record(**overrides):
    """一条完整的系统订单记录（已受理的开仓市价单），字段可按名覆盖。"""
    from dmr_kernel.core.types import OrderPurpose, OrderSide, OrderState, PositionSide
    from dmr_kernel.ledger.records import OrderRecord

    fields: dict[str, Any] = dict(
        client_order_id="DKA-1-1-o", intent_id=1, leg="o", purpose=OrderPurpose.OPEN, symbol="BTCUSDT",
        side=OrderSide.BUY, position_side=PositionSide.LONG, kind="market", order_type="MARKET", qty=D("0.5"),
        trigger_price=None, working_type=None, close_position=False, exchange_order_id="1001", algo_id=None,
        actual_order_id=None, state=OrderState.ACKNOWLEDGED, raw_status="NEW", filled_qty=D("0"), avg_price=None,
        error_code=None, error_msg=None, sent_ms=T0, acked_ms=T0, last_query_ms=None, absent_count=0, resend_of=None,
        created_ms=T0, updated_ms=T0 + 5)
    fields.update(overrides)
    return OrderRecord(**fields)


# ---------------------------------------------------------------- 只有登录与席位服务


@dataclass
class AuthRig:
    ledger: Any
    clock: ManualClock
    bus: EventBus
    config: dict[str, Any]
    auth: AuthService
    seat: SeatService
    local_token: str
    seat_events: list[SeatChanged] = field(default_factory=list)

    @classmethod
    def make(cls, ledger, clock, bus, *, limiter: LoginLimiter | None = None, params: ScryptParams = FAST) -> "AuthRig":
        config = default_config()
        local_token = secrets.token_urlsafe(32)
        service = AuthService(ledger, clock, bus, config_get=config.__getitem__, local_token=local_token,
                              params=params, limiter=limiter)
        seat = SeatService(ledger, clock, bus, config_get=config.__getitem__)
        rig = cls(ledger, clock, bus, config, service, seat, local_token)
        bus.subscribe(SeatChanged, rig.seat_events.append, name="test-seat-events")
        return rig

    async def operator(self, username: str = "alice", password: str = PASSWORD, role: str = "admin") -> OperatorRecord:
        return await self.auth.create_operator(username, password, role=role)

    async def login(self, username: str = "alice", password: str = PASSWORD) -> LoginResult:
        return await self.auth.login(username, password)

    async def principal(self, username: str = "alice", password: str = PASSWORD, *, create: bool = True) -> Principal:
        """登录并返回认证后的主体；操作员不存在就先建。"""
        if create and self.ledger.read_sync(repo_auth.get_operator_by_name, username) is None:
            await self.operator(username, password)
        result = await self.login(username, password)
        return await self.auth.authenticate(result.token)

    async def settle(self) -> list[SeatChanged]:
        await self.bus.start()
        await self.bus.drain()
        return list(self.seat_events)


# ---------------------------------------------------------------- 整个应用


class Sess:
    """一个已登录会话的小助手：带着令牌发请求。"""

    def __init__(self, kit: "Kit", username: str, token: str, session_id: int) -> None:
        self.kit, self.username, self.token, self.session_id = kit, username, token, session_id

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path: str, **kw):
        return self.kit.get(path, token=self.token, **kw)

    def post(self, path: str, json: Any = None, **kw):
        return self.kit.post(path, json=json, token=self.token, **kw)

    def put(self, path: str, json: Any = None, **kw):
        return self.kit.put(path, json=json, token=self.token, **kw)


class Kit:
    @classmethod
    @contextmanager
    def open(cls, ledger, clock: ManualClock, tmp_path: Path, *, env: str = "standin",
             raise_server_exceptions: bool = True, ws_ping_s: float = 3600.0, ws_max_queue: int = 1000):
        from starlette.testclient import TestClient

        kit = cls(ledger, clock, tmp_path, env=env, ws_ping_s=ws_ping_s, ws_max_queue=ws_max_queue)
        with TestClient(kit.app, raise_server_exceptions=raise_server_exceptions) as client:
            kit.client = client
            kit.run(kit.boot())
            try:
                yield kit
            finally:
                kit.run(kit.bus.stop())

    def __init__(self, ledger, clock: ManualClock, tmp_path: Path, *, env: str = "standin",
                 ws_ping_s: float = 3600.0, ws_max_queue: int = 1000) -> None:
        from dmr_kernel.alerts.notifier import Notifier
        from dmr_kernel.api.app import ApiContext, create_app
        from dmr_kernel.core.config import ConfigStore
        from dmr_kernel.core.limits import Limits
        from dmr_kernel.core.modelstate import ModelStateService
        from dmr_kernel.core.registry import Registry
        from dmr_kernel.core.riskgate import RiskGate
        from dmr_kernel.core.types import Environment, Model
        from dmr_kernel.models.m1_strategy import StrategyModel
        from dmr_kernel.models.m2_takeover import TakeoverModel
        from dmr_kernel.models.m3_reserved import ReservedModel
        from dmr_kernel.settings import load_settings

        self.ledger, self.clock, self.tmp_path = ledger, clock, tmp_path
        self.bus = EventBus()
        self.settings = load_settings({"DMR_VAR_DIR": str(tmp_path / "var"), "DMR_ENV": env}, webui_root=tmp_path)
        self.config = ConfigStore(ledger, self.bus, clock, Environment.STANDIN, deploy_switch=False)
        self.modelstate = ModelStateService(ledger, self.bus, clock)
        self.registry = Registry(ledger, self.bus, clock, config_get=self.config.get)
        self.notifier = Notifier(ledger, self.bus, clock)
        self.riskgate = RiskGate(ledger, self.bus, clock, config_get=self.config.get, notifier=self.notifier)
        self.limits = Limits(clock, config_get=self.config.get)
        self.local_token = secrets.token_urlsafe(32)
        self.heartbeat_path = tmp_path / "var" / "kernel" / "heartbeat.json"
        self.ctx = ApiContext(
            settings=self.settings, clock=clock, ledger=ledger, bus=self.bus, modelstate=self.modelstate,
            registry=self.registry, config=self.config, riskgate=self.riskgate, limits=self.limits,
            notifier=self.notifier,
            models={Model.M1: StrategyModel(), Model.M2: TakeoverModel(), Model.M3: ReservedModel()},
            local_token=self.local_token, heartbeat_path=self.heartbeat_path, ws_ping_s=ws_ping_s,
            ws_max_queue=ws_max_queue, scrypt_params=FAST)
        self.app = create_app(self.ctx)
        self.services = self.app.state.services
        self.client = None

    # ------------------------------------------------------------ 在应用的事件循环里执行

    async def boot(self) -> None:
        await self.config.load()
        await self.modelstate.load()
        await self.registry.load()
        await self.riskgate.load()
        await self.bus.start()

    def run(self, coro) -> Any:
        async def runner():
            return await coro

        return self.client.portal.call(runner)

    def publish(self, *events) -> None:
        """在应用的事件循环里发布事件，并等总线把它们都送到订阅者。"""

        async def go():
            for event in events:
                self.bus.publish(event)
            await self.bus.drain()

        self.run(go())

    def settle(self) -> None:
        self.run(self.bus.drain())

    # ------------------------------------------------------------ 请求

    @staticmethod
    def _headers(token: str | None, headers: dict[str, str] | None) -> dict[str, str]:
        merged = dict(headers or {})
        if token is not None:
            merged["Authorization"] = f"Bearer {token}"
        return merged

    def request(self, method: str, path: str, *, token: str | None = None, headers: dict[str, str] | None = None,
                **kw):
        return self.client.request(method, BASE + path, headers=self._headers(token, headers), **kw)

    def get(self, path: str, *, token: str | None = None, **kw):
        return self.request("GET", path, token=token, **kw)

    def post(self, path: str, *, token: str | None = None, json: Any = None, **kw):
        return self.request("POST", path, token=token, json=json, **kw)

    def put(self, path: str, *, token: str | None = None, json: Any = None, **kw):
        return self.request("PUT", path, token=token, json=json, **kw)

    # ------------------------------------------------------------ 数据

    def add_operator(self, username: str = "admin", password: str = PASSWORD, role: str = "admin") -> OperatorRecord:
        return self.ledger.write_sync(repo_auth.create_operator, username=username,
                                      password_hash=auth_mod.hash_password(password, FAST), role=role,
                                      now_ms=self.clock.now_ms())

    def login(self, username: str = "admin", password: str = PASSWORD, *, create: bool = True) -> Sess:
        if create and self.ledger.read_sync(repo_auth.get_operator_by_name, username) is None:
            self.add_operator(username, password)
        response = self.post("/auth/login", json={"username": username, "password": password})
        assert response.status_code == 200, response.text
        body = response.json()
        return Sess(self, username, body["token"], body["session_id"])

    def cli(self) -> Sess:
        return Sess(self, "cli", self.local_token, 0)

    def add_alert(self, level, kind: str, message: str, *, at_ms: int | None = None, delivered: bool = True):
        return self.ledger.write_sync(repo_alerts.insert, at_ms=self.clock.now_ms() if at_ms is None else at_ms,
                                      level=level, kind=kind, message=message, delivered=delivered)

    def add_audit(self, *, action: str, operator: str = "admin", target: str = "", before=None, after=None,
                  result: str = "ok", request_id: str | None = None, at_ms: int | None = None):
        return self.ledger.write_sync(repo_audit.append, at_ms=self.clock.now_ms() if at_ms is None else at_ms,
                                      operator=operator, action=action, target=target, before=before, after=after,
                                      request_id=request_id, result=result)

    def add_order(self, client_order_id: str, *, symbol: str = "BTCUSDT", position_side=None, side=None,
                  purpose=None, kind: str = "market", order_type: str = "MARKET", qty: Decimal | None = D("0.01"),
                  trigger_price: Decimal | None = None, intent_id: int | None = None, leg: str = "o"):
        from dmr_kernel.core.types import OrderPurpose, OrderSide, PositionSide

        record, _ = self.ledger.write_sync(
            repo_orders.create_pending, client_order_id=client_order_id, intent_id=intent_id, leg=leg,
            purpose=purpose or OrderPurpose.OPEN, symbol=symbol, side=side or OrderSide.BUY,
            position_side=position_side or PositionSide.LONG, kind=kind, order_type=order_type, qty=qty,
            trigger_price=trigger_price, working_type=None, close_position=False, resend_of=None,
            now_ms=self.clock.now_ms())
        return record

    def add_position(self, *, symbol: str = "BTCUSDT", side=None, qty: Decimal = D("0.5"),
                     avg_price: Decimal = D("60000"), ownership=None, intent_id: int = 1):
        """登记一条托管中的策略仓（经登记簿，所以缓存与账本一致）。"""
        from dmr_kernel.core.types import Ownership, PositionSide

        return self.run(self.registry.register_open(
            symbol=symbol, side=side or PositionSide.LONG, qty=qty, avg_price=avg_price,
            open_time_ms=self.clock.now_ms(), ownership=ownership or Ownership.STRATEGY, model="m1",
            intent_id=intent_id, entry_reason="test", leverage=2, margin_mode="isolated"))

    def add_pending_position(self, *, symbol: str = "ETHUSDT", side=None, qty: Decimal = D("2"),
                             avg_price: Decimal = D("3000")):
        from dmr_kernel.core.types import PendingReason, PositionSide

        return self.run(self.registry.register_pending(
            symbol=symbol, side=side or PositionSide.SHORT, qty=qty, avg_price=avg_price,
            reason=PendingReason.OUT_OF_SCOPE, flags=["manual"], note="来源不明"))

    def dump(self, exclude: tuple[str, ...] = ("session",)) -> dict[str, list[tuple]]:
        """每张业务表的全部内容（独立的只读连接）；session 表默认不比（读请求会推进它的最近活动时间）。"""
        with closing(sqlite3.connect(f"file:{self.ledger.path}?mode=ro", uri=True, isolation_level=None,
                                     timeout=5)) as conn:
            tables = [name for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "AND name != 'alembic_version' ORDER BY name") if name not in exclude]
            return {name: conn.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall() for name in tables}

    def row_counts(self) -> dict[str, int]:
        """每张业务表的行数（独立的只读连接）。"""
        with closing(sqlite3.connect(f"file:{self.ledger.path}?mode=ro", uri=True, isolation_level=None,
                                     timeout=5)) as conn:
            tables = [name for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "AND name != 'alembic_version' ORDER BY name")]
            return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables}
