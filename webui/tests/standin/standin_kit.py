"""替身交易所测试的公共帮手（只在 tests/standin 里用）。

不连任何网络：行情一律用 StaticFeed，时间一律用可调的假时钟。
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from dmr_standin.dispatcher import Dispatcher, build_standin
from dmr_standin.engine import Engine
from dmr_standin.faults import Switches
from dmr_standin.feed import InstrumentSpec, StaticFeed

D = Decimal
# 2026-10-14 02:00:00 UTC：不在资金费结算时刻上
T0 = 1_791_943_200_000
KERNEL_TOKEN = "k" * 43
MANUAL_TOKEN = "m" * 43
KH = {"X-Standin-Key": KERNEL_TOKEN}
MH = {"X-Standin-Key": MANUAL_TOKEN}


class FakeClock:
    """返回 UTC 毫秒的可调用对象；只在 advance/set 时变化。"""

    def __init__(self, ms: int = T0):
        self.ms = int(ms)

    def __call__(self) -> int:
        return self.ms

    def advance(self, ms: int) -> None:
        self.ms += int(ms)

    def set(self, ms: int) -> None:
        self.ms = int(ms)


def spec(symbol: str, tick: str, step: str, min_qty: str, min_notional: str, *, status: str = "TRADING",
         base: str | None = None, market_step: str | None = None, market_max: str | None = "1000000",
         ref_price: str | None = None) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol, base=base or symbol.removesuffix("USDT"), quote="USDT", contract_type="PERPETUAL",
        status=status, tick_size=D(tick), step_size=D(step), min_qty=D(min_qty), min_notional=D(min_notional),
        market_step_size=D(market_step or step), market_max_qty=D(market_max) if market_max else None,
        onboard_ms=1_569_398_400_000, ref_price=None if ref_price is None else D(ref_price),
    )


def instruments() -> list[InstrumentSpec]:
    return [
        spec("BTCUSDT", "0.1", "0.001", "0.001", "100"),
        spec("ETHUSDT", "0.01", "0.001", "0.001", "20"),
        spec("DOGEUSDT", "0.00001", "1", "1", "5"),
    ]


PRICES = {"BTCUSDT": "50000", "ETHUSDT": "3000", "DOGEUSDT": "0.2"}


def make_feed(clock: FakeClock, prices: dict[str, str] | None = None) -> StaticFeed:
    feed = StaticFeed(instruments(), clock=clock)
    for sym, px in (PRICES if prices is None else prices).items():
        feed.set_price(sym, px)
    return feed


def make_engine(*, equity: str = "1000", prices: dict[str, str] | None = None,
                **behaviors: Any) -> tuple[Engine, StaticFeed, Switches, FakeClock]:
    clock = FakeClock()
    feed = make_feed(clock, prices)
    switches = Switches(clock)
    if behaviors:
        switches.update({"behaviors": behaviors})
    engine = Engine(feed=feed, switches=switches, clock=clock, equity=D(equity), seed=7)
    return engine, feed, switches, clock


def make_standin(*, equity: str = "1000", prices: dict[str, str] | None = None, state_dir=None,
                 persist: bool = False, sleep=None) -> tuple[Dispatcher, StaticFeed, FakeClock]:
    clock = FakeClock()
    feed = make_feed(clock, prices)
    kwargs: dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    disp = build_standin(feed=feed, clock=clock, equity=D(equity), kernel_token=KERNEL_TOKEN,
                         manual_token=MANUAL_TOKEN, state_dir=state_dir, persist=persist, seed=7, **kwargs)
    return disp, feed, clock


async def call(disp: Dispatcher, method: str, path: str, *, params: dict | None = None, body: Any = None,
               headers: dict | None = None) -> tuple[int, Any, dict[str, str]]:
    """按 HTTP 外壳的方式调用分发器：请求体编码成 JSON 字节。"""
    raw = None if body is None else json.dumps(body).encode()
    return await disp.handle(method, path, params or {}, raw, headers if headers is not None else KH)


def open_long(engine: Engine, symbol: str = "BTCUSDT", qty: str = "0.01", cid: str = "DKA1-1-1-o") -> dict:
    return engine.place_market_order(symbol=symbol, side="BUY", position_side="LONG", qty=D(qty),
                                     client_order_id=cid)


def open_short(engine: Engine, symbol: str = "BTCUSDT", qty: str = "0.01", cid: str = "DKA1-1-2-o") -> dict:
    return engine.place_market_order(symbol=symbol, side="SELL", position_side="SHORT", qty=D(qty),
                                     client_order_id=cid)


def position(engine: Engine, symbol: str, side: str) -> dict | None:
    for p in engine.positions():
        if p["symbol"] == symbol and p["position_side"] == side:
            return p
    return None


def stop(engine: Engine, *, symbol: str = "BTCUSDT", side: str = "SELL", position_side: str = "LONG",
         trigger: str = "49000", working_type: str = "CONTRACT_PRICE", close_position: bool = True,
         qty: str | None = None, cid: str = "DKA1-0-1-s", algo_type: str = "STOP_MARKET") -> dict:
    return engine.place_algo_order(symbol=symbol, side=side, position_side=position_side, algo_type=algo_type,
                                   trigger_price=D(trigger), working_type=working_type,
                                   close_position=close_position, qty=None if qty is None else D(qty),
                                   client_algo_id=cid)
