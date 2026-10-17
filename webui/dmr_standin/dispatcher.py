"""替身交易所与传输无关的请求分发与数据流订阅中心（docs/设计约定.md 7.1–7.5）。

HTTP 外壳（``server.py``）与内核测试的进程内传输（``tests/support/``）都直接用下面这组接口，走同一套请求解析与撮合代码；
改名字或语义时要同步改这两处::

    disp = build_standin(feed=..., clock=None, equity=Decimal("1000"), kernel_token=None, manual_token=None,
                         state_dir=None, persist=True, seed=None, sleep=asyncio.sleep)
    status, body, headers = await disp.handle(method, path, params, body, headers)
    sub = disp.subscribe(listen_key)        # 账户数据流；拒绝时抛 stream.StreamRefused
    frame = await sub.get()                 # 下一帧（7.3 的 JSON 对象）；断开且取空后抛 stream.StreamClosed
    frames = sub.drain(); sub.close()       # 同步取走已到的帧；客户端主动断开
    disp.tick()                             # 推进一次（后台每秒一次；测试直接调）
    await disp.start(); await disp.stop()   # 后台任务：每秒 tick、每 2 秒轮询行情、变化后 1 秒内落盘；stop 时落盘并断开数据流
    disp.flush()                            # 立即落盘（没有持久化时什么也不做）
    disp.engine, disp.feed, disp.switches, disp.hub

``build_standin``：
- ``clock``：返回 UTC 毫秒的可调用对象，或带 ``now_ms()`` 的对象（如内核的 ``ManualClock``）；缺省为系统时间。
- 令牌：给出 ``kernel_token``/``manual_token`` 就用它们；否则从 ``state_dir`` 读取或生成（``persistence.ensure_tokens``）。
- ``persist`` 且有 ``state_dir`` 时：从 ``state.json`` 恢复（损坏抛 ValueError）、操作留痕写 ``ops.log``；否则全在内存。
- ``sleep``：故障「扣住」用的等待函数（缺省 ``asyncio.sleep``）。

``handle(method, path, params, body, headers)``：
- ``method``：GET/POST/PUT/DELETE；``path``：如 ``/api/private/order``；``params``：查询参数（字符串到字符串）；
  ``body``：请求体字节（JSON 对象；也接受 ``str``、已解析的 ``dict`` 或 ``None``）；``headers``：请求头（名字不分大小写）。
- 返回 ``(status, body, headers)``：``body`` 是可 JSON 编码的对象（成功的报文，或失败时的 ``{"code", "msg"}``）；
  ``headers`` 总有 ``X-MBX-USED-WEIGHT-1M``（滚动 1 分钟的已用权重，含本次），限频时另有 ``Retry-After``（秒）。
- 鉴权：``/api/private/*`` 只认 kernel 令牌，``/dev/*`` 只认 manual 令牌（请求头 ``X-Standin-Key``），否则 401。
- 「扣住不答」类故障（``order_timeout``、``position_query_fail`` 的 hang）表现为协程迟迟不返回，调用方按自己的超时取消；
  ``order_timeout`` 的 drop 在扣住 ``hang_s`` 之后抛 ``Disconnect``（外壳应当不给完整响应、直接断开连接）。
- 故障只作用于内核用的接口（``/api/*``）；``/dev/*`` 的模拟手动操作不受故障影响、不计权重、不限频，但每次都写留痕。
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import re
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .engine import Engine, EngineError, PartialFill
from .faults import SwitchError, Switches
from .feed import BINANCE_INTERVALS, MAX_KLINES, FeedError, to_decimal
from .persistence import STATE_VERSION, OpsLog, StateStore, ensure_tokens
from .stream import StreamHub, Subscription

log = logging.getLogger("dmr_standin")

WEIGHT_HEADER = "X-MBX-USED-WEIGHT-1M"
WEIGHT_WINDOW_MS = 60_000
TICK_INTERVAL_S = 1.0
POLL_INTERVAL_S = 2.0
MARKS_EVERY = 5  # 每 5 次价格轮询（10 秒）连同标记价与资金费率
PERSIST_CHECK_S = 0.25
HANG_FOREVER_S = 3600.0
UNKNOWN_MSG = "Timeout waiting for response from backend server. Send status unknown; execution status unknown."

Sleep = Callable[[float], Awaitable[Any]]


class Disconnect(Exception):
    """``order_timeout`` 的 drop 故障：外壳应当不给完整响应、直接断开连接。"""


class _BadRequest(Exception):
    def __init__(self, code: int, msg: str, http_status: int = 400):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg
        self.http_status = http_status


def system_clock() -> int:
    return time.time_ns() // 1_000_000


def _as_ms_clock(clock: Any) -> Callable[[], int]:
    if clock is None:
        return system_clock
    now_ms = getattr(clock, "now_ms", None)
    if callable(now_ms):
        return now_ms
    if callable(clock):
        return clock
    raise TypeError("clock 必须是返回毫秒的可调用对象，或带 now_ms() 的对象")


def _err(code: int, msg: str) -> dict[str, Any]:
    return {"code": code, "msg": msg}


# ---------------------------------------------------------------- 参数解析


def _parse_body(body: Any) -> dict[str, Any]:
    if body is None or body == b"" or body == "":
        return {}
    if isinstance(body, Mapping):
        return dict(body)
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError, TypeError):
        raise _BadRequest(-1102, "请求体不是合法的 JSON") from None
    if not isinstance(data, dict):
        raise _BadRequest(-1102, "请求体必须是 JSON 对象")
    return data


def _str(src: Mapping[str, Any], name: str, *, required: bool = True) -> str | None:
    value = src.get(name)
    if value is None or value == "":
        if required:
            raise _BadRequest(-1102, f"缺少参数 {name}")
        return None
    if not isinstance(value, str):
        raise _BadRequest(-1102, f"参数 {name} 必须是字符串")
    return value


def _int(src: Mapping[str, Any], name: str, *, required: bool = False) -> int | None:
    value = src.get(name)
    if value is None or value == "":
        if required:
            raise _BadRequest(-1102, f"缺少参数 {name}")
        return None
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    raise _BadRequest(-1102, f"参数 {name} 必须是整数")


def _num(src: Mapping[str, Any], name: str, *, required: bool = True) -> Decimal | None:
    value = src.get(name)
    if value is None:
        if required:
            raise _BadRequest(-1102, f"缺少参数 {name}")
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _BadRequest(-1102, f"参数 {name} 必须是数值")
    try:
        return to_decimal(value)
    except ValueError:
        raise _BadRequest(-1102, f"参数 {name} 必须是数值：{value!r}") from None


@dataclass
class _Request:
    method: str
    path: str
    params: dict[str, str]
    data: dict[str, Any]
    now: int


@dataclass(frozen=True)
class _Route:
    handler: str
    auth: str  # public | kernel | manual
    weight: int | Callable[[Mapping[str, str]], int] = 0
    op: str | None = None  # 开发操作的留痕名


def _by_symbol(with_symbol: int, without: int) -> Callable[[Mapping[str, str]], int]:
    return lambda params: with_symbol if params.get("symbol") else without


ROUTES: dict[tuple[str, str], _Route] = {
    # 公共
    ("GET", "/api/public/time"): _Route("_time", "public", 1),
    ("GET", "/api/public/instruments"): _Route("_instruments", "public", 1),
    ("GET", "/api/public/klines"): _Route("_klines", "public", 1),
    ("GET", "/api/public/tickers"): _Route("_tickers", "public", lambda p: 1 if p.get("symbols") else 2),
    ("GET", "/api/public/feed_status"): _Route("_feed_status", "public", 1),
    # 私有
    ("GET", "/api/private/account"): _Route("_account", "kernel", 5),
    ("GET", "/api/private/position_mode"): _Route("_position_mode", "kernel", 30),
    ("GET", "/api/private/positions"): _Route("_positions", "kernel", 5),
    ("GET", "/api/private/open_orders"): _Route("_open_orders", "kernel", _by_symbol(1, 40)),
    ("GET", "/api/private/open_algo_orders"): _Route("_open_algo_orders", "kernel", _by_symbol(1, 40)),
    ("GET", "/api/private/order"): _Route("_get_order", "kernel", 1),
    ("GET", "/api/private/algo_order"): _Route("_get_algo_order", "kernel", 1),
    ("POST", "/api/private/order"): _Route("_post_order", "kernel", 0),
    ("POST", "/api/private/algo_order"): _Route("_post_algo_order", "kernel", 0),
    ("DELETE", "/api/private/order"): _Route("_delete_order", "kernel", 1),
    ("DELETE", "/api/private/algo_order"): _Route("_delete_algo_order", "kernel", 1),
    ("GET", "/api/private/trades"): _Route("_trades", "kernel", 5),
    ("GET", "/api/private/income"): _Route("_income", "kernel", 30),
    ("GET", "/api/private/leverage"): _Route("_get_leverage", "kernel", 1),
    ("POST", "/api/private/leverage"): _Route("_post_leverage", "kernel", 1),
    ("POST", "/api/private/margin_mode"): _Route("_post_margin_mode", "kernel", 1),
    ("GET", "/api/private/trading_status"): _Route("_trading_status", "kernel", 1),
    ("POST", "/api/private/listen_key"): _Route("_post_listen_key", "kernel", 1),
    ("PUT", "/api/private/listen_key"): _Route("_put_listen_key", "kernel", 1),
    # 开发控制（7.4）
    ("POST", "/dev/manual/order"): _Route("_manual_order", "manual", op="manual_order"),
    ("POST", "/dev/manual/algo_order"): _Route("_manual_algo_order", "manual", op="manual_algo_order"),
    ("POST", "/dev/manual/cancel"): _Route("_manual_cancel", "manual", op="manual_cancel"),
    ("GET", "/dev/state"): _Route("_dev_state", "manual"),
    ("GET", "/dev/ops_log"): _Route("_dev_ops_log", "manual"),
    ("GET", "/dev/faults"): _Route("_get_faults", "manual"),
    ("PUT", "/dev/faults"): _Route("_put_faults", "manual", op="faults_update"),
    ("POST", "/dev/faults/reset"): _Route("_reset_faults", "manual", op="faults_reset"),
    ("POST", "/dev/price_override"): _Route("_price_override", "manual", op="price_override"),
    ("POST", "/dev/funding/settle"): _Route("_funding_settle", "manual", op="funding_settle"),
    ("POST", "/dev/subscribe"): _Route("_dev_subscribe", "manual", op="subscribe"),
    ("POST", "/dev/reset"): _Route("_dev_reset", "manual", op="reset"),
}


class Dispatcher:
    """见模块文档。一般用 ``build_standin`` 构造。"""

    def __init__(self, *, engine: Engine, feed: Any, switches: Switches, hub: StreamHub, clock: Callable[[], int],
                 kernel_token: str, manual_token: str, store: StateStore | None = None, ops: OpsLog | None = None,
                 subscribed: set[str] | None = None, sleep: Sleep = asyncio.sleep):
        if not kernel_token or not manual_token or kernel_token == manual_token:
            raise ValueError("kernel 与 manual 两把令牌都必须给出且互不相同")
        self.engine = engine
        self.feed = feed
        self.switches = switches
        self.hub = hub
        self._clock = clock
        self._kernel_token = kernel_token
        self._manual_token = manual_token
        self._store = store
        self.ops = ops or OpsLog(None)
        self._subscribed: set[str] = set(subscribed or ())
        self._subscribed_version = 0
        self._sleep = sleep
        self._weights: deque[tuple[int, int]] = deque()
        self._weight_sum = 0
        self._tasks: list[asyncio.Task] = []
        self._saved_marker: tuple[int, int, int] | None = None

    # ------------------------------------------------------------ 对外接口

    async def handle(self, method: str, path: str, params: Mapping[str, Any] | None, body: Any,
                     headers: Mapping[str, Any] | None) -> tuple[int, Any, dict[str, str]]:
        method = str(method).upper()
        path = "/" + str(path).strip().strip("/")
        query = {str(k): str(v) for k, v in (params or {}).items() if v is not None}
        lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        now = self._clock()
        route = ROUTES.get((method, path))
        weight = 0
        if route is not None and route.auth != "manual":
            weight = route.weight(query) if callable(route.weight) else route.weight
        out = {WEIGHT_HEADER: str(self._use_weight(now, weight))}
        if route is None:
            return 404, _err(-1000, f"未知的接口：{method} {path}"), out
        expected = self._kernel_token if route.auth == "kernel" else self._manual_token
        if route.auth != "public" and not self._token_ok(lowered.get("x-standin-key"), expected):
            who = "kernel" if route.auth == "kernel" else "manual"
            return 401, _err(-2015, f"令牌无效：这个接口只认 {who} 令牌（请求头 X-Standin-Key）"), out
        if route.auth != "manual":
            kind = "reads" if method == "GET" else "writes"
            hit = self.switches.consume("rate_limit", lambda c: c["scope"] in (kind, "all"))
            if hit is not None:
                out["Retry-After"] = str(self._retry_after(now, hit["http"]))
                return hit["http"], _err(-1003, f"请求过于频繁（故障开关 rate_limit，HTTP {hit['http']}）"), out
        data: dict[str, Any] = {}
        status: int
        payload: Any
        try:
            if method in ("POST", "PUT"):
                data = _parse_body(body)
            req = _Request(method, path, query, data, now)
            status, payload = await getattr(self, route.handler)(req)
        except (EngineError, FeedError, _BadRequest) as exc:
            status, payload = exc.http_status, _err(exc.code, exc.msg)
        except SwitchError as exc:
            status, payload = 400, _err(-1102, str(exc))
        finally:
            self._publish()
        if route.op is not None:
            self._log_op(route.op, data, status, payload)
        return status, payload, out

    def subscribe(self, listen_key: Any) -> Subscription:
        return self.hub.subscribe(listen_key)

    def tick(self) -> None:
        self.switches.stream_drop()  # 到期的断线窗口自动关闭
        self.engine.check_triggers()
        self.engine.maybe_settle_funding()
        self._publish()
        self.hub.tick()
        self.feed.set_interest(self.engine.interesting_symbols() | self._subscribed)

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [asyncio.create_task(self._tick_loop(), name="standin-tick"),
                       asyncio.create_task(self._poll_loop(), name="standin-poll")]
        if self._store is not None:
            self._tasks.append(asyncio.create_task(self._persist_loop(), name="standin-persist"))

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            self.flush()
        except OSError:
            log.exception("替身停机前落盘失败")
        self.hub.close_all("server_stopping")

    def flush(self) -> None:
        if self._store is None:
            return
        marker = self._marker()
        self._store.save(self.state_snapshot())
        self._saved_marker = marker

    def state_snapshot(self) -> dict[str, Any]:
        """``state.json`` 的内容：引擎快照 + 行为开关 + /dev/subscribe 订阅的合约（故障开关不存）。"""
        return {"version": STATE_VERSION, "saved_ms": self._clock(), "engine": self.engine.to_snapshot(),
                "behaviors": self.switches.behaviors(), "subscribed": sorted(self._subscribed)}

    # ------------------------------------------------------------ 内部：后台任务

    def _marker(self) -> tuple[int, int, int]:
        return self.engine.version, self.switches.version, self._subscribed_version

    async def _tick_loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("替身交易所 tick 出错")
            await asyncio.sleep(TICK_INTERVAL_S)

    async def _poll_loop(self) -> None:
        i = 0
        while True:
            try:
                await self.feed.poll_once(marks=(i % MARKS_EVERY == 0))
            except Exception as exc:  # 行情源自己记录错误；这里只防止后台任务退出
                log.warning("行情轮询失败：%s", exc)
            i += 1
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _persist_loop(self) -> None:
        while True:
            await asyncio.sleep(PERSIST_CHECK_S)
            if self._marker() != self._saved_marker:
                try:
                    self.flush()
                except Exception:
                    log.exception("替身状态落盘失败")

    # ------------------------------------------------------------ 内部：公共设施

    def _publish(self) -> None:
        frames = self.engine.drain_events()
        if frames:
            self.hub.publish(frames)

    @staticmethod
    def _token_ok(given: str | None, expected: str) -> bool:
        return given is not None and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))

    def _use_weight(self, now: int, weight: int) -> int:
        while self._weights and self._weights[0][0] <= now - WEIGHT_WINDOW_MS:
            self._weight_sum -= self._weights.popleft()[1]
        if weight:
            self._weights.append((now, weight))
            self._weight_sum += weight
        return self._weight_sum

    def used_weight(self) -> int:
        return self._use_weight(self._clock(), 0)

    @staticmethod
    def _retry_after(now: int, http: int) -> int:
        if http == 418:
            return 120
        return max(1, math.ceil((WEIGHT_WINDOW_MS - now % WEIGHT_WINDOW_MS) / 1000))

    def _log_op(self, op: str, request: dict[str, Any], status: int, payload: Any) -> None:
        entry: dict[str, Any] = {"time_ms": self._clock(), "op": op, "request": request,
                                 "result": "ok" if status == 200 else "error", "status": status}
        if status != 200 and isinstance(payload, Mapping):
            entry["code"], entry["msg"] = payload.get("code"), payload.get("msg")
        try:
            self.ops.append(entry)
        except OSError:
            log.exception("写操作留痕失败")

    async def _ensure_quotes(self, symbols: list[str]) -> None:
        try:
            await self.feed.ensure_quotes(symbols)
        except FeedError:
            pass  # 取不到价时由引擎按「没有行情」拒绝

    def _known(self, symbol: str) -> str:
        if self.feed.instrument(symbol) is None:
            raise _BadRequest(-1121, f"未知的合约：{symbol}")
        return symbol

    async def _hang_then(self, cfg: dict[str, Any] | None, result: tuple[int, Any]) -> tuple[int, Any]:
        if cfg is not None:
            await self._sleep(cfg["hang_s"])
        return result

    async def _drop(self, cfg: dict[str, Any]) -> None:
        await self._sleep(cfg["hang_s"])
        log.info("故障开关 order_timeout=drop：请求未受理，扣住 %ss 后断开连接", cfg["hang_s"])
        raise Disconnect("order_timeout drop：不受理，扣住后断开")

    # ------------------------------------------------------------ 公共接口

    async def _time(self, req: _Request) -> tuple[int, Any]:
        return 200, {"server_time_ms": req.now}

    async def _instruments(self, req: _Request) -> tuple[int, Any]:
        return 200, [i.to_json() for i in self.feed.instruments()]

    async def _klines(self, req: _Request) -> tuple[int, Any]:
        symbol = self._known(_str(req.params, "symbol"))
        interval = _str(req.params, "interval", required=False) or "15m"
        if interval not in BINANCE_INTERVALS:
            raise _BadRequest(-1120, f"不支持的 K 线周期：{interval}")
        limit = _int(req.params, "limit")
        limit = MAX_KLINES if limit is None else limit
        if not 1 <= limit <= MAX_KLINES:
            raise _BadRequest(-1102, f"limit 必须是 1 到 {MAX_KLINES} 的整数")
        return 200, await self.feed.klines(symbol, interval, limit)

    async def _tickers(self, req: _Request) -> tuple[int, Any]:
        raw = _str(req.params, "symbols", required=False)
        if raw:
            symbols = [self._known(s.strip()) for s in raw.split(",") if s.strip()]
            await self._ensure_quotes(symbols)
        else:
            symbols = self.feed.symbols()
        out = {}
        for s in symbols:
            q = self.engine.quote(s)
            if q is not None:
                out[s] = q
        return 200, out

    async def _feed_status(self, req: _Request) -> tuple[int, Any]:
        return 200, self.feed.status()

    # ------------------------------------------------------------ 私有接口：查询

    async def _account(self, req: _Request) -> tuple[int, Any]:
        return 200, self.engine.account()

    async def _position_mode(self, req: _Request) -> tuple[int, Any]:
        return 200, {"dual_side": True}

    async def _positions(self, req: _Request) -> tuple[int, Any]:
        fail = self.switches.consume("position_query_fail")
        if fail is not None:
            if fail["mode"] == "error":
                return 500, _err(-1001, "持仓查询失败（故障开关 position_query_fail）")
            await self._sleep(HANG_FOREVER_S)
            raise Disconnect("position_query_fail hang")
        return 200, self.engine.positions()

    async def _open_orders(self, req: _Request) -> tuple[int, Any]:
        symbol = _str(req.params, "symbol", required=False)
        return 200, self.engine.open_orders(self._known(symbol) if symbol else None)

    async def _open_algo_orders(self, req: _Request) -> tuple[int, Any]:
        symbol = _str(req.params, "symbol", required=False)
        return 200, self.engine.open_algo_orders(self._known(symbol) if symbol else None)

    async def _get_order(self, req: _Request) -> tuple[int, Any]:
        p = req.params
        return 200, self.engine.get_order(symbol=_str(p, "symbol"), order_id=p.get("order_id") or None,
                                          client_order_id=p.get("client_order_id") or None)

    async def _get_algo_order(self, req: _Request) -> tuple[int, Any]:
        p = req.params
        return 200, self.engine.get_algo_order(symbol=_str(p, "symbol"), algo_id=p.get("algo_id") or None,
                                               client_algo_id=p.get("client_algo_id") or None)

    async def _trades(self, req: _Request) -> tuple[int, Any]:
        p = req.params
        symbol = _str(p, "symbol", required=False)
        return 200, self.engine.trades(symbol=self._known(symbol) if symbol else None,
                                       start_ms=_int(p, "start_ms"), end_ms=_int(p, "end_ms"), limit=_int(p, "limit"))

    async def _income(self, req: _Request) -> tuple[int, Any]:
        p = req.params
        return 200, self.engine.income(income_type=_str(p, "type", required=False),
                                       symbol=_str(p, "symbol", required=False), start_ms=_int(p, "start_ms"),
                                       end_ms=_int(p, "end_ms"), limit=_int(p, "limit"))

    async def _get_leverage(self, req: _Request) -> tuple[int, Any]:
        return 200, self.engine.leverage_info(self._known(_str(req.params, "symbol")))

    async def _trading_status(self, req: _Request) -> tuple[int, Any]:
        return 200, {"restricted": False, "detail": {}}

    # ------------------------------------------------------------ 私有接口：写

    def _order_args(self, d: Mapping[str, Any]) -> dict[str, Any]:
        order_type = d.get("type", "MARKET")
        if order_type != "MARKET":
            raise _BadRequest(-1102, f"替身只支持市价单（type=MARKET）：{order_type!r}")
        return {"symbol": d.get("symbol"), "side": d.get("side"), "position_side": d.get("position_side"),
                "qty": _num(d, "qty")}

    def _algo_args(self, d: Mapping[str, Any]) -> dict[str, Any]:
        return {"symbol": d.get("symbol"), "side": d.get("side"), "position_side": d.get("position_side"),
                "algo_type": d.get("type"), "trigger_price": _num(d, "trigger_price"),
                "working_type": d.get("working_type", "CONTRACT_PRICE"),
                "close_position": d.get("close_position", False), "qty": _num(d, "qty", required=False)}

    async def _post_order(self, req: _Request) -> tuple[int, Any]:
        args = self._order_args(req.data)
        timeout = self.switches.consume("order_timeout", lambda c: c["scope"] in ("market", "all"))
        if timeout is not None and timeout["mode"] == "drop":
            await self._drop(timeout)
        partial = None
        pf = self.switches.fault("partial_fill")
        if pf["enabled"]:
            partial = PartialFill(ratio=Decimal(pf["ratio"]), remainder=pf["remainder"],
                                  delay_ms=int(round(pf["delay_s"] * 1000)))
        if isinstance(args["symbol"], str):
            await self._ensure_quotes([args["symbol"]])
        try:
            order = self.engine.place_market_order(**args, client_order_id=req.data.get("client_order_id"),
                                                   partial=partial)
            if partial is not None and order["status"] == "PARTIALLY_FILLED":
                self.switches.consume("partial_fill")
            result: tuple[int, Any] = (200, order)
        except EngineError as exc:
            result = (exc.http_status, _err(exc.code, exc.msg))
        self._publish()
        return await self._hang_then(timeout, result)

    async def _post_algo_order(self, req: _Request) -> tuple[int, Any]:
        args = self._algo_args(req.data)
        timeout = self.switches.consume("order_timeout", lambda c: c["scope"] in ("algo", "all"))
        if timeout is not None and timeout["mode"] == "drop":
            await self._drop(timeout)
        fail = self.switches.consume("algo_create_fail")
        if fail is not None:
            if fail["mode"] == "reject":
                result: tuple[int, Any] = (400, _err(fail["code"], "止损建单失败（故障开关 algo_create_fail）"))
            else:
                result = (503, _err(-1007, UNKNOWN_MSG))
            return await self._hang_then(timeout, result)
        if isinstance(args["symbol"], str):
            await self._ensure_quotes([args["symbol"]])
        try:
            result = (200, self.engine.place_algo_order(**args, client_algo_id=req.data.get("client_algo_id")))
        except EngineError as exc:
            result = (exc.http_status, _err(exc.code, exc.msg))
        self._publish()
        return await self._hang_then(timeout, result)

    async def _cancel(self, req: _Request, fn: Callable[..., dict[str, Any]],
                      id_names: tuple[str, str]) -> tuple[int, Any]:
        p = req.params
        symbol = _str(p, "symbol")
        ids = {name: p.get(name) or None for name in id_names}
        timeout = self.switches.consume("order_timeout", lambda c: c["scope"] == "all")
        if timeout is not None and timeout["mode"] == "drop":
            await self._drop(timeout)
        try:
            result: tuple[int, Any] = (200, fn(symbol=symbol, **ids))
        except EngineError as exc:
            result = (exc.http_status, _err(exc.code, exc.msg))
        self._publish()
        return await self._hang_then(timeout, result)

    async def _delete_order(self, req: _Request) -> tuple[int, Any]:
        return await self._cancel(req, self.engine.cancel_order, ("order_id", "client_order_id"))

    async def _delete_algo_order(self, req: _Request) -> tuple[int, Any]:
        return await self._cancel(req, self.engine.cancel_algo_order, ("algo_id", "client_algo_id"))

    async def _post_leverage(self, req: _Request) -> tuple[int, Any]:
        d = req.data
        return 200, self.engine.set_leverage(self._known(_str(d, "symbol")), _int(d, "leverage", required=True))

    async def _post_margin_mode(self, req: _Request) -> tuple[int, Any]:
        d = req.data
        return 200, self.engine.set_margin_mode(self._known(_str(d, "symbol")), d.get("margin_mode"))

    async def _post_listen_key(self, req: _Request) -> tuple[int, Any]:
        key, expires = self.hub.keys.create(req.now)
        return 200, {"listen_key": key, "expires_ms": expires}

    async def _put_listen_key(self, req: _Request) -> tuple[int, Any]:
        key = _str(req.data, "listen_key")
        if self.switches.fault("listen_key_keepalive_fail")["enabled"]:
            return 400, _err(-1125, "listenKey 续期失败（故障开关 listen_key_keepalive_fail）")
        expires = self.hub.keys.keepalive(key, req.now)
        if expires is None:
            return 400, _err(-1125, "listenKey 不存在或已失效")
        return 200, {"expires_ms": expires}

    # ------------------------------------------------------------ 开发控制（7.4）

    async def _manual_order(self, req: _Request) -> tuple[int, Any]:
        d = req.data
        args = {"symbol": d.get("symbol"), "side": d.get("side"), "position_side": d.get("position_side"),
                "qty": _num(d, "qty")}
        if isinstance(args["symbol"], str):
            await self._ensure_quotes([args["symbol"]])
        return 200, self.engine.place_market_order(**args, client_order_id=self.engine.new_web_id(),
                                                   source="manual")

    async def _manual_algo_order(self, req: _Request) -> tuple[int, Any]:
        args = self._algo_args(req.data)
        if isinstance(args["symbol"], str):
            await self._ensure_quotes([args["symbol"]])
        return 200, self.engine.place_algo_order(**args, client_algo_id=self.engine.new_web_id(), source="manual")

    async def _manual_cancel(self, req: _Request) -> tuple[int, Any]:
        d = req.data
        symbol = _str(d, "symbol")
        if d.get("client_order_id") or d.get("order_id"):
            return 200, self.engine.cancel_order(symbol=symbol, order_id=d.get("order_id") or None,
                                                 client_order_id=d.get("client_order_id") or None)
        if d.get("client_algo_id") or d.get("algo_id"):
            return 200, self.engine.cancel_algo_order(symbol=symbol, algo_id=d.get("algo_id") or None,
                                                      client_algo_id=d.get("client_algo_id") or None)
        raise _BadRequest(-1102, "需要 client_order_id、order_id、client_algo_id、algo_id 之一")

    async def _dev_state(self, req: _Request) -> tuple[int, Any]:
        engine = self.engine
        watched = sorted(engine.interesting_symbols() | self._subscribed | set(engine.overrides()))
        prices = {s: q for s in watched if (q := engine.quote(s)) is not None}
        return 200, {
            "time_ms": req.now, "account": engine.account(), "positions": engine.positions(),
            "open_orders": engine.open_orders(), "open_algo_orders": engine.open_algo_orders(), "prices": prices,
            "price_overrides": engine.overrides(), "feed": self.feed.status(), "switches": self.switches.to_json(),
            "subscribed": sorted(self._subscribed), "used_weight_1m": self.used_weight(),
            "stream": {"subscriptions": self.hub.count(), "listen_key_active": self.hub.keys.active(req.now)},
        }

    async def _dev_ops_log(self, req: _Request) -> tuple[int, Any]:
        limit = _int(req.params, "limit")
        limit = 100 if limit is None else limit
        if not 1 <= limit <= 1000:
            raise _BadRequest(-1102, "limit 必须是 1 到 1000 的整数")
        return 200, self.ops.recent(limit)

    async def _get_faults(self, req: _Request) -> tuple[int, Any]:
        return 200, self.switches.to_json()

    async def _put_faults(self, req: _Request) -> tuple[int, Any]:
        self.switches.update(req.data)
        return 200, self.switches.to_json()

    async def _reset_faults(self, req: _Request) -> tuple[int, Any]:
        self.switches.reset()
        return 200, self.switches.to_json()

    async def _price_override(self, req: _Request) -> tuple[int, Any]:
        d = req.data
        symbol = self._known(_str(d, "symbol"))
        if d.get("clear") is True:
            quote = self.engine.set_price_override(symbol, clear=True)
        elif d.get("price") is not None:
            quote = self.engine.set_price_override(symbol, price=_num(d, "price"))
        elif d.get("pct") is not None:
            await self._ensure_quotes([symbol])
            quote = self.engine.set_price_override(symbol, pct=_num(d, "pct"))
        else:
            raise _BadRequest(-1102, "需要 price、pct 或 clear=true 之一")
        self.engine.check_triggers()  # 覆盖后立即检查条件单，不必等下一次 tick
        return 200, {"symbol": symbol, "quote": quote, "overrides": self.engine.overrides()}

    async def _funding_settle(self, req: _Request) -> tuple[int, Any]:
        symbol = _str(req.data, "symbol", required=False)
        return 200, self.engine.settle_funding(self._known(symbol) if symbol else None)

    async def _dev_subscribe(self, req: _Request) -> tuple[int, Any]:
        symbols = req.data.get("symbols")
        if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
            raise _BadRequest(-1102, "symbols 必须是合约名的列表")
        for s in symbols:
            self._known(s)
        self._subscribed |= set(symbols)
        self._subscribed_version += 1
        return 200, {"subscribed": sorted(self._subscribed)}

    async def _dev_reset(self, req: _Request) -> tuple[int, Any]:
        equity = _num(req.data, "equity", required=False)
        self.engine.reset(equity if equity is not None else Decimal("1000"))
        return 200, {"account": self.engine.account()}


def build_standin(*, feed: Any, clock: Any = None, equity: Decimal | str = Decimal("1000"),
                  kernel_token: str | None = None, manual_token: str | None = None,
                  state_dir: Path | str | None = None, persist: bool = True, seed: int | None = None,
                  sleep: Sleep = asyncio.sleep) -> Dispatcher:
    """装配替身交易所（参数见模块文档）。"""
    ms_clock = _as_ms_clock(clock)
    if kernel_token is None or manual_token is None:
        if state_dir is None:
            raise ValueError("没有令牌：请给出 kernel_token 与 manual_token，或给出 state_dir 由替身生成")
        kernel, manual = ensure_tokens(state_dir)
        kernel_token = kernel_token or kernel
        manual_token = manual_token or manual
    switches = Switches(ms_clock)
    store = StateStore(state_dir) if persist and state_dir is not None else None
    ops = OpsLog(Path(state_dir) / "ops.log" if store is not None else None)
    snap = store.load() if store is not None else None
    subscribed: set[str] = set()
    if snap is not None:
        engine = Engine.from_snapshot(snap["engine"], feed=feed, switches=switches, clock=ms_clock, seed=seed)
        switches.load_behaviors(snap.get("behaviors") or {})
        subscribed = {s for s in snap.get("subscribed") or [] if isinstance(s, str)}
    else:
        engine = Engine(feed=feed, switches=switches, clock=ms_clock, equity=equity, seed=seed)
    hub = StreamHub(clock=ms_clock, switches=switches)
    disp = Dispatcher(engine=engine, feed=feed, switches=switches, hub=hub, clock=ms_clock,
                      kernel_token=kernel_token, manual_token=manual_token, store=store, ops=ops,
                      subscribed=subscribed, sleep=sleep)
    if snap is not None:
        disp._saved_marker = disp._marker()
    return disp
