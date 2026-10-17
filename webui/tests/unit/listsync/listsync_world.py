"""名单同步测试的帮手：样例夹具与变体、假 HTTP、假告警器、合约清单。测试里绝不联网。"""
from __future__ import annotations

import copy
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "onlycoin_live_sample.json"
URL = "http://127.0.0.1:18080/api/v1/onlycoin/live"


def iso_ms(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def sample() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


SAMPLE_NOW_MS = iso_ms("2026-10-16T22:17:30Z")  # 样例里 server_time 之后几秒；valid_until 是 22:30


def member(symbol: str, direction: str, score: float, first: str = "2026-10-16T01:15:00Z", **extra: Any) -> dict[str, Any]:
    return {"symbol": symbol, "direction": direction, "total_score": score, "first_selected_at": first,
            "expires_at_utc": "2026-10-16T01:30:00Z", "scan_id": "20261016-005", **extra}


def payload(onlycoin=(), long=(), short=(), **top: Any) -> dict[str, Any]:
    """在样例的顶层字段上换掉三个成员数组（long、short 缺省按 onlycoin 的方向拆）。"""
    body = sample()
    onlycoin = list(onlycoin)
    body["onlycoin"] = onlycoin
    body["long"] = list(long) if long else [m for m in onlycoin if m["direction"] == "LONG"]
    body["short"] = list(short) if short else [m for m in onlycoin if m["direction"] == "SHORT"]
    body["symbols"] = [m["symbol"] for m in onlycoin]
    body["status"] = "live" if onlycoin else "empty"
    body.update(top)
    return body


def variant(**top: Any) -> dict[str, Any]:
    body = copy.deepcopy(sample())
    body.update(top)
    return body


class FakeHttp:
    """按顺序给出预设的 (状态码, 正文) 或要抛出的异常；记录每次请求。"""

    def __init__(self, *responses: Any):
        self.responses = list(responses)
        self.calls: list[tuple[str, float]] = []
        self.closed = False

    def push(self, *responses: Any) -> None:
        self.responses.extend(responses)

    async def get_json(self, url: str, *, timeout_s: float):
        self.calls.append((url, timeout_s))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


class FakeNotifier:
    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []

    def raise_alert(self, level, kind, message, *, dedupe_key=None) -> None:
        self.alerts.append({"level": level, "kind": kind, "message": message, "dedupe_key": dedupe_key})

    def kinds(self) -> list[str]:
        return [a["kind"] for a in self.alerts]


def make_instrument(symbol: str, *, status: str = "TRADING", contract_type: str = "PERPETUAL", quote: str = "USDT"):
    from dmr_kernel.core.types import Instrument

    base = symbol[: -len(quote)] if symbol.endswith(quote) else symbol
    return Instrument(symbol=symbol, base=base, quote=quote, contract_type=contract_type, status=status,
                      tick_size=Decimal("0.0001"), step_size=Decimal("1"), min_qty=Decimal("1"),
                      min_notional=Decimal("5"))


SAMPLE_SYMBOLS = ("TIAUSDT", "APRUSDT", "MUBARAKUSDT", "LUMIAUSDT", "PYTHUSDT", "ZKUSDT")
