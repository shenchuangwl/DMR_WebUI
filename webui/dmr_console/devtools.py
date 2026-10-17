"""替身开发工具的客户端（只在替身环境用）：直接调替身交易所的 ``/dev/manual/*``、``/dev/faults``、``/dev/price_override``。

- 地址：环境变量 ``DMR_STANDIN_URL``，缺省 ``http://127.0.0.1:18220``；只允许回环上的 http。
- 令牌：``var/standin/manual.token``，每次请求前现读（令牌轮换后不用重启控制台），放在请求头 ``X-Standin-Key``；
  令牌不进异常文字、日志或 repr。
- 与内核无关：这些操作绕过内核，模拟「用户在交易所手动下单」；控制台自己在日志里留一行。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from dmr_console.settings import DEFAULT_STANDIN_URL, SettingsError, is_loopback_host, parse_standin_url

log = logging.getLogger(__name__)

TOKEN_HEADER = "X-Standin-Key"
TIMEOUT_S = 5.0

ORDER_ACTIONS = {"open": "开仓", "add": "加仓", "reduce": "减仓"}
ALGO_KINDS = {"stop": "止损", "take_profit": "止盈"}
DIRECTIONS = {"LONG": "多 LONG", "SHORT": "空 SHORT"}
_ALGO_TYPES = {"stop": "STOP_MARKET", "take_profit": "TAKE_PROFIT_MARKET"}
FAULT_KEYS = ("order_timeout", "algo_create_fail", "position_query_fail", "partial_fill", "stream_drop", "rate_limit",
              "listen_key_keepalive_fail")
FAULT_LABELS = {
    "order_timeout": "下单超时", "algo_create_fail": "条件单创建失败", "position_query_fail": "持仓查询失败",
    "partial_fill": "部分成交", "stream_drop": "数据流断线", "rate_limit": "限频", "listen_key_keepalive_fail": "监听凭据续期失败",
}


class StandinError(Exception):
    """调替身失败；文字是给人看的中文，不含令牌。"""


def standin_url_from_env(env: Mapping[str, str]) -> str:
    try:
        return parse_standin_url(env.get("DMR_STANDIN_URL") or "")
    except SettingsError as exc:
        raise ValueError(str(exc)) from None


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "http" or not is_loopback_host(parts.hostname or ""):
        raise ValueError(f"替身交易所只允许回环上的 http，拒绝 {url!r}")


def _direction(direction: str) -> str:
    if direction not in DIRECTIONS:
        raise ValueError(f"方向只能是 LONG 或 SHORT：{direction!r}")
    return direction


def _closing_side(direction: str) -> str:
    return "SELL" if direction == "LONG" else "BUY"


def manual_order_body(action: str, symbol: str, direction: str, qty: str) -> dict:
    """开仓、加仓：与持仓同向；减仓：反向（双向持仓模式，position_side 指明哪一侧）。"""
    if action not in ORDER_ACTIONS:
        raise ValueError(f"操作只能是开仓、加仓、减仓：{action!r}")
    direction = _direction(direction)
    closing = _closing_side(direction)
    opening = "BUY" if direction == "LONG" else "SELL"
    return {"symbol": symbol, "side": closing if action == "reduce" else opening, "position_side": direction, "qty": qty}


def manual_algo_body(kind: str, symbol: str, direction: str, trigger_price: str) -> dict:
    """止损 / 止盈：全平型条件单（close_position），在平仓方向上。"""
    if kind not in _ALGO_TYPES:
        raise ValueError(f"条件单只能是止损或止盈：{kind!r}")
    direction = _direction(direction)
    return {"symbol": symbol, "side": _closing_side(direction), "position_side": direction, "type": _ALGO_TYPES[kind],
            "trigger_price": trigger_price, "close_position": True}


class StandinDev:
    def __init__(self, base_url: str, token_path: Path, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout_s: float = TIMEOUT_S) -> None:
        _check_url(base_url)
        self._token_path = Path(token_path)
        self._timeout_s = timeout_s
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/", transport=transport, timeout=timeout_s,
                                       follow_redirects=False, trust_env=False)

    def __repr__(self) -> str:
        return f"StandinDev(base_url={str(self._http.base_url)!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    def _token(self) -> str:
        try:
            token = self._token_path.read_text(encoding="utf-8").strip()
        except OSError:
            raise StandinError(f"读不到手动令牌文件 {self._token_path.name}：替身交易所还没启动过，或目录不对") from None
        if not token:
            raise StandinError(f"手动令牌文件 {self._token_path.name} 是空的")
        return token

    async def _call(self, method: str, path: str, body: Any = None) -> dict:
        headers = {TOKEN_HEADER: self._token()}
        try:
            response = await asyncio.wait_for(
                self._http.request(method, path.lstrip("/"), headers=headers, json=body), timeout=self._timeout_s)
        except (TimeoutError, httpx.HTTPError, OSError):
            raise StandinError("连不上替身交易所（或请求超时）：请确认它在运行") from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            msg = payload.get("msg") if isinstance(payload, dict) else None
            raise StandinError(f"替身交易所拒绝了请求（HTTP {response.status_code}）：{msg}" if isinstance(msg, str)
                               else f"替身交易所返回 HTTP {response.status_code}")
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------ 模拟手动下单

    async def manual_order(self, action: str, symbol: str, direction: str, qty: str) -> dict:
        return await self._call("POST", "/dev/manual/order", manual_order_body(action, symbol, direction, qty))

    async def manual_algo(self, kind: str, symbol: str, direction: str, trigger_price: str) -> dict:
        return await self._call("POST", "/dev/manual/algo_order", manual_algo_body(kind, symbol, direction, trigger_price))

    # ------------------------------------------------------------ 故障开关与价格覆盖

    async def faults(self) -> dict:
        return await self._call("GET", "/dev/faults")

    async def set_fault(self, key: str, enabled: bool) -> dict:
        if key not in FAULT_KEYS:
            raise ValueError(f"未知的故障开关：{key!r}")
        return await self._call("PUT", "/dev/faults", {"faults": {key: {"enabled": enabled}}})

    async def reset_faults(self) -> dict:
        return await self._call("POST", "/dev/faults/reset")

    async def price_override(self, symbol: str, *, price: str | None = None, pct: str | None = None, clear: bool = False) -> dict:
        body: dict[str, Any] = {"symbol": symbol}
        if clear:
            body["clear"] = True
        elif price is not None:
            body["price"] = price
        elif pct is not None:
            body["pct"] = pct
        else:
            raise ValueError("价格覆盖需要 price、pct 或 clear 之一")
        return await self._call("POST", "/dev/price_override", body)
