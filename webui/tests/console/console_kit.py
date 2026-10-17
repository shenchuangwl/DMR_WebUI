"""控制台测试的公共夹具（只给 tests/console 用）。

- ``ManualClock``：手动推进的毫秒时钟（控制台不得导入 dmr_kernel，所以自备一个）。
- ``make_state`` / ``make_position`` / ``make_oplog_item`` / ``make_alert``：按 docs/plans/M1-控制接口只读部分.md 造 JSON 样例。
- ``FakeKernel``：HTTP 级假内核，配 ``httpx.MockTransport``，用来测真正的 ``KernelClient``（超时、失联、报文解析）。
- ``FakeKernelClient``：接口级假客户端，用来测页面状态逻辑与控制器（失联时按钮禁用、状态条字段齐全）。
"""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from dmr_console.client import KernelError, KernelUnreachable, LoginResult

START_MS = 1_760_000_000_000  # 2025-10-09T08:53:20Z
API = "/api/v1"
GOOD_TOKEN = "tok-secret-0001"


class ManualClock:
    def __init__(self, now_ms: int = START_MS) -> None:
        self._now = now_ms

    def now_ms(self) -> int:
        return self._now

    def advance(self, seconds: float = 0, *, ms: int = 0) -> int:
        self._now += int(seconds * 1000) + ms
        return self._now


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _allowed(ok: bool, reason: str = "") -> dict:
    return {"allowed": ok, "reason": reason, "needs_live_read": False, "note": ""}


def make_state(**over: Any) -> dict:
    """GET /state 的样例（3.1 节）；关键字参数按顶层字段深度合并。"""
    base: dict[str, Any] = {
        "env": "standin", "standin": True, "time_ms": START_MS, "heartbeat_ms": START_MS - 5_000, "kernel_version": "0.1.0",
        "model": {
            "active": "m1", "gate": "stopped", "version": 12,
            "suppressions": [], "op_locks": [],
            "run_mode": {"code": "normal", "label": "正常"},
        },
        "allowed": {
            "start": _allowed(True), "stop": _allowed(False, "开仓闸已关闭"), "switch": _allowed(True),
            "switch_confirm": _allowed(False, "没有待确认的切换"), "switch_cancel": _allowed(False, "没有待确认的切换"),
            "partial_close": _allowed(False, "没有托管持仓"), "close_one": _allowed(False, "没有托管持仓"),
            "close_all": _allowed(False, "没有托管持仓"), "abort": _allowed(False, "没有进行中的批量操作"),
        },
        "button_meanings": {
            "start": "打开开仓闸，按信号自动开仓", "stop": "关上开仓闸，撤掉未成交的开仓单",
            "partial_close": "对任何一个托管持仓部分平仓", "close_all": "默认平策略仓",
        },
        "account": {"known": True, "equity": "1000", "available": "900", "wallet": "1000", "at_ms": START_MS - 2_000},
        "counts": {"managed": 0, "unprotected": 0, "taking_over": 0, "closing": 0, "pending": 0, "external": 0},
        "list": {"known": False, "valid": False, "reason": "名单同步在 M4 接入", "member_count": 0, "valid_until_ms": None},
        "stream": {"known": True, "state": "connected", "last_frame_ms": START_MS - 1_000},
        "reconcile": {"positions": {"last_ms": START_MS - 4_000, "status": "ok"},
                      "orders": {"last_ms": START_MS - 4_000, "status": "ok"}},
        "position_mode": {"known": True, "dual_side": True},
        "restrictions": {"known": True, "restricted": False},
        "risk": {
            "loaded": True, "stale": False, "account_tripped": False, "daily_loss_tripped": False, "drawdown_tripped": False,
            "model_tripped": False, "daily_loss_limit": "50", "daily_loss_remaining": "50", "max_drawdown_pct": "10",
            "drawdown_remaining_pct": "10.00", "trades_remaining": 50, "model_loss_remaining": "50",
            "reasons": [], "limits_missing": [],
        },
        "limits": {"used": 0, "reserved": 0, "cap": 30, "stale": False},
        "seat": {"holder_session_id": None, "holder_operator": None, "lease_until_ms": None, "mine": False},
        "me": {"operator": "admin", "principal": "session", "reauth_valid_until_ms": None},
    }
    return deep_merge(base, over)


def make_position(**over: Any) -> dict:
    base: dict[str, Any] = {
        "id": 1, "symbol": "BTCUSDT", "side": "LONG", "generation": 1, "status": "managed", "ownership": "strategy",
        "is_mixed": False, "qty": "0.010", "avg_price": "60000", "mark_price": "60100", "unrealized_pnl": "1.00",
        "liquidation_price": "30000", "protection_state": "system", "stop_price": "58000", "stop_note": "",
        "exit_ruleset": "strategy", "signal_exit": True, "open_time_ms": START_MS - 3_600_000, "open_time_estimated": False,
        "exchange_qty": "0.010", "consistent": True, "has_unconfirmed_orders": False, "closing": False,
        "pending_reason": None, "pending_flags": [], "origin_note": "", "leverage": 5, "margin_mode": "cross",
        "has_stop_on_exchange": True, "version": 3,
    }
    return deep_merge(base, over)


def make_oplog_item(**over: Any) -> dict:
    base: dict[str, Any] = {
        "id": 10, "at_ms": START_MS - 60_000, "operator": "admin", "action": "login", "target": "session",
        "result": "ok", "request_id": "req-10", "before": None, "after": None,
    }
    return deep_merge(base, over)


def make_alert(**over: Any) -> dict:
    base: dict[str, Any] = {"id": 5, "at_ms": START_MS - 30_000, "level": "warning", "kind": "stream_gap",
                            "message": "数据流中断 3 秒", "delivered": True}
    return deep_merge(base, over)


def make_watchlist(**over: Any) -> dict:
    """GET /watchlist 的样例（M4 计划第 4 节的报文）。"""
    base: dict[str, Any] = {
        "valid": True, "reason": "", "fetched_ms": START_MS - 3_000, "valid_until_ms": START_MS + 3_600_000,
        "business_date": "2025-10-09",
        "members": [
            {"symbol": "BTCUSDT", "direction": "LONG", "score": "8.5", "first_selected_ms": START_MS - 7_200_000,
             "held": True, "avoid_reason": None},
            {"symbol": "1000PEPEUSDT", "direction": "SHORT", "score": "7.25", "first_selected_ms": START_MS - 3_600_000,
             "held": False, "avoid_reason": "外部仓：该合约已有手动持仓"},
        ],
        "rejected": [{"code": "某某币", "reason": "交易所里查不到这个合约"}],
    }
    base.update(over)
    return base


def make_trade(**over: Any) -> dict:
    """GET /trades 的一笔（M5 计划 T4.1：字段同 trade 表，数值是字符串、时间是毫秒整数，另加 duration_ms 与 return_pct）。"""
    base: dict[str, Any] = {
        "id": 1, "position_id": 1, "symbol": "BTCUSDT", "side": "LONG", "ownership": "strategy", "model": "m1", "is_mixed": False,
        "open_ms": START_MS - 7_200_000, "close_ms": START_MS - 3_600_000, "open_time_estimated": False,
        "max_qty": "0.010", "open_avg": "60000", "close_avg": "61000", "open_notional": "600",
        "gross_pnl": "10", "fees": "0.5", "funding": "0.1", "net_pnl": "9.4", "entry_reason": "名单做多 BTCUSDT（分数 8.5）",
        "exit_reason": "stop", "list_snapshot_id": 3, "list_direction": "LONG", "list_score": "8.5",
        "duration_ms": 3_600_000, "return_pct": "1.5667",
    }
    return deep_merge(base, over)


def make_trades(*items: dict) -> dict:
    rows = list(items)
    return {"items": rows, "total": len(rows)}


def make_stat_block(**over: Any) -> dict:
    """一组统计（M5 计划 T4.2）。盈亏比可为 null；三个比率是 0–1 之间的小数字符串。"""
    base: dict[str, Any] = {
        "count": 4, "profit_loss_ratio": "2.5", "profit_rate": "0.0125", "win_rate": "0.5",
        "net_pnl": "7.5", "gross_pnl": "9", "fees": "1", "funding": "0.5", "avg_duration_ms": 5_400_000,
        "max_win": "10", "max_loss": "-4",
    }
    base.update(over)
    return base


def make_stats(**over: Any) -> dict:
    base: dict[str, Any] = {
        "overall": make_stat_block(),
        "groups": [{"key": "m1", "stats": make_stat_block()}],
        "mixed": make_stat_block(count=1, net_pnl="-2"),
    }
    base.update(over)
    return base


def make_candles(n: int = 6, **over: Any) -> dict:
    """GET /candles 的样例：n 根 15m K 线，DMR 前 2 个为 null。"""
    step = 900_000
    first = START_MS - n * step
    candles = [[first + i * step, str(100 + i), str(105 + i), str(95 + i), str(102 + i), "10"] for i in range(max(n, 5))][:n or 0]
    full = [first + i * step for i in range(max(n, 5))]
    base: dict[str, Any] = {
        "candles": candles,
        "dmr": [[c[0], None if i < 2 else str(0.1 * i)] for i, c in enumerate(candles)],
        "trades": [{"position_id": 1, "side": "LONG", "open_ms": full[1] + 60_000, "open_price": "101",
                    "close_ms": full[4] + 60_000, "close_price": "104", "exit_reason": "stop"}],
        "stops": [{"position_id": 1, "price": "98", "from_ms": full[1] + 60_000, "to_ms": full[3]},
                  {"position_id": 1, "price": "100", "from_ms": full[3], "to_ms": full[4] + 60_000}],
    }
    base.update(over)
    return base


def envelope(code: str, message: str, **extra: Any) -> dict:
    return {"code": code, "message": message, **extra}


# ------------------------------------------------------------------ HTTP 级假内核

Reply = Callable[[httpx.Request], Any]


class FakeKernel:
    """按（方法, 路径）脚本化响应；记录每个请求。路径不含 /api/v1 前缀。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: dict[tuple[str, str], Reply] = {}

    # --- 脚本
    def reply(self, method: str, path: str, body: Any = None, status: int = 200, *, raw: bytes | None = None,
              headers: dict[str, str] | None = None) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            if raw is not None:
                return httpx.Response(status, content=raw, headers=headers)
            return httpx.Response(status, json=body, headers=headers)
        self._routes[(method.upper(), path)] = handler

    def error(self, method: str, path: str, status: int, code: str, message: str = "错误", **extra: Any) -> None:
        self.reply(method, path, envelope(code, message, **extra), status)

    def raise_on(self, method: str, path: str, exc: BaseException) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            raise exc
        self._routes[(method.upper(), path)] = handler

    def hang(self, method: str, path: str) -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")
        self._routes[(method.upper(), path)] = handler

    # --- 传输
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> Any:
        self.requests.append(request)
        path = request.url.path.removeprefix(API)
        handler = self._routes.get((request.method, path))
        if handler is None:
            return httpx.Response(404, json=envelope("not_found", f"没有这个接口：{path}"))
        return handler(request)

    # --- 断言帮手
    def requests_to(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method.upper() and r.url.path == API + path]


# ------------------------------------------------------------------ 接口级假客户端

@dataclass
class FakeKernelClient:
    """与 ``KernelClient`` 同形的异步接口；结果可脚本化为返回值、异常或可调用对象。"""

    valid_tokens: set[str] = field(default_factory=lambda: {GOOD_TOKEN})
    accounts: dict[str, str] = field(default_factory=lambda: {"admin": "pw-admin"})
    script: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, tuple, dict]] = field(default_factory=list)
    posts: list[tuple[str, Any]] = field(default_factory=list)
    puts: list[tuple[str, Any]] = field(default_factory=list)
    delay_s: float = 0.0
    closed: bool = False

    # --- 脚本帮手
    def set(self, method: str, value: Any) -> None:
        self.script[method] = value

    def fail_all(self, exc: BaseException) -> None:
        for name in ("health", "state", "positions", "positions_external", "orders", "oplog", "alerts", "config", "login",
                     "logout", "me"):
            self.script[name] = exc

    def heal(self) -> None:
        self.script.clear()

    def calls_of(self, name: str) -> list[tuple]:
        return [args for (n, args, _) in self.calls if n == name]

    # --- 内部
    async def _answer(self, name: str, args: tuple, kwargs: dict, default: Any, *, token: str | None = None,
                      needs_token: bool = True) -> Any:
        self.calls.append((name, args, kwargs))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        value = self.script.get(name, default)
        if isinstance(value, BaseException):
            raise value
        if needs_token and token not in self.valid_tokens:
            raise KernelError(401, "unauthorized", "会话无效或已过期")
        if callable(value):
            value = value(*args, **kwargs)
            if asyncio.iscoroutine(value):
                value = await value
        if isinstance(value, BaseException):
            raise value
        return copy.deepcopy(value)

    # --- 接口
    async def health(self) -> dict:
        return await self._answer("health", (), {}, {"status": "ok", "env": "standin", "version": "0.1.0",
                                                    "time_ms": START_MS}, needs_token=False)

    async def login(self, username: str, password: str) -> LoginResult:
        def check(u: str, p: str) -> LoginResult:
            if self.accounts.get(u) != p:
                raise KernelError(401, "bad_credentials", "用户名或口令错误")
            return LoginResult(token=GOOD_TOKEN, operator=u, session_id="sess-1", idle_timeout_min=30)
        return await self._answer("login", (username, password), {}, check, needs_token=False)

    async def logout(self, token: str) -> None:
        self.valid_tokens.discard(token)
        await self._answer("logout", (token,), {}, {}, needs_token=False)

    async def me(self, token: str) -> dict:
        return await self._answer("me", (token,), {}, {"operator": "admin", "session_id": "sess-1", "principal": "session",
                                                         "reauth_valid_until_ms": None,
                                                         "seat": make_state()["seat"]}, token=token)

    async def state(self, token: str) -> dict:
        return await self._answer("state", (token,), {}, make_state(), token=token)

    async def positions(self, token: str) -> list[dict]:
        return await self._answer("positions", (token,), {}, [], token=token)

    async def positions_external(self, token: str) -> list[dict]:
        return await self._answer("positions_external", (token,), {}, [], token=token)

    async def orders(self, token: str) -> dict:
        return await self._answer("orders", (token,), {}, {"system": [], "exchange_open": [], "exchange_algo": [],
                                                           "snapshot_ms": START_MS}, token=token)

    async def oplog(self, token: str, *, limit: int = 100, before_id: int | None = None) -> list[dict]:
        return await self._answer("oplog", (token,), {"limit": limit, "before_id": before_id}, [], token=token)

    async def alerts(self, token: str, *, limit: int = 100) -> list[dict]:
        return await self._answer("alerts", (token,), {"limit": limit}, [], token=token)

    async def config(self, token: str) -> list[dict]:
        return await self._answer("config", (token,), {}, [], token=token)

    async def watchlist(self, token: str) -> dict:
        return await self._answer("watchlist", (token,), {}, make_watchlist(), token=token)

    async def trades(self, token: str, **query: Any) -> dict:
        return await self._answer("trades", (token,), query, make_trades(), token=token)

    async def stats(self, token: str, **query: Any) -> dict:
        return await self._answer("stats", (token,), query, make_stats(), token=token)

    async def candles(self, token: str, **query: Any) -> dict:
        return await self._answer("candles", (token,), query, make_candles(), token=token)

    async def post(self, token: str, path: str, body: Any = None) -> dict:
        """操作类接口（POST）。脚本：``script["POST " + path]`` 可以是返回值、异常或 ``f(body)``；缺省返回成功信封。"""
        self.posts.append((path, copy.deepcopy(body)))
        default = {"op_id": (body or {}).get("op_id") if isinstance(body, dict) else None, "status": "succeeded", "result": {}}
        return await self._answer("POST " + path, (body,), {}, default, token=token)

    async def put_config(self, token: str, key: str, body: Any) -> dict:
        """``PUT /config/{key}``。脚本：``script["PUT /config/" + key]``；缺省返回成功（新值即提交的值）。"""
        self.puts.append((key, copy.deepcopy(body)))
        value = body.get("value") if isinstance(body, dict) else None
        default = {"key": key, "old": None, "new": value, "applies": "立即生效", "affects_existing": False}
        return await self._answer("PUT /config/" + key, (body,), {}, default, token=token)

    def puts_to(self, key: str) -> list[dict]:
        return [b for (k, b) in self.puts if k == key]

    async def operation(self, token: str, op_id: str) -> dict:
        return await self._answer("GET /operations", (op_id,), {}, {"op_id": op_id, "status": "running", "per_position": None},
                                  token=token)

    def posts_to(self, path: str) -> list[dict]:
        return [b for (p, b) in self.posts if p == path]

    async def aclose(self) -> None:
        self.closed = True


def lost(kind: str = "connect") -> KernelUnreachable:
    reason = {"connect": "无法连接内核", "timeout": "请求内核超时", "protocol": "内核响应无法解析"}[kind]
    return KernelUnreachable(reason, kind)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
