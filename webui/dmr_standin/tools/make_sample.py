"""一次性生成 ``dmr_standin/data/instruments_sample.json``（计划 T0.5「行情源」；回放行情用的合约规则样例）。

只用 Binance U 本位合约的两个公共 GET：``fapiPublicGetExchangeInfo``（合约清单）与 ``fapiPublicGetTickerPrice``
（最新价，作 ``ref_price``），都经 ``feed.BinancePublicFeed``：白名单方法、不配任何密钥、单实例长连接、每个请求超时。
不在禁网包装下运行（在 webui 根目录）::

    .venv-kernel/bin/python -B -m dmr_standin.tools.make_sample [--out PATH]

选取：当日名单里的合约（USUSDT、QUSDT、BEAMXUSDT、BATUSDT、STXUSDT）、BTCUSDT、ETHUSDT、SOLUSDT、BNBUSDT、
XRPUSDT、DOGEUSDT、1000PEPEUSDT，再加一个中文合约名（若存在：USDT 永续、交易中、合约名含汉字，按合约名排序取第一个）。
合约清单里没有的、或取不到最新价的，跳过并在标准错误里列出。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ..feed import SAMPLE_PATH, BinancePublicFeed, InstrumentSpec, to_decimal

LIST_SYMBOLS = ("USUSDT", "QUSDT", "BEAMXUSDT", "BATUSDT", "STXUSDT")
MAJOR_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "1000PEPEUSDT")


def _clock() -> int:
    return time.time_ns() // 1_000_000


def _has_han(text: str) -> bool:
    return any("一" <= ch <= "鿿" or "㐀" <= ch <= "䶿" for ch in text)


def choose(instruments: dict[str, InstrumentSpec],
           prices: dict[str, Decimal]) -> tuple[list[InstrumentSpec], list[str]]:
    """按模块文档选取合约；返回 (带 ref_price 的合约规则, 跳过的合约名及原因)。"""
    wanted = list(LIST_SYMBOLS + MAJOR_SYMBOLS)
    han = sorted(s for s, spec in instruments.items()
                 if _has_han(s) and spec.contract_type == "PERPETUAL" and spec.quote == "USDT"
                 and spec.status == "TRADING" and s in prices)
    if han:
        wanted.append(han[0])
    chosen: list[InstrumentSpec] = []
    skipped: list[str] = []
    for symbol in wanted:
        spec = instruments.get(symbol)
        if spec is None:
            skipped.append(f"{symbol}：合约清单里没有")
        elif symbol not in prices:
            skipped.append(f"{symbol}：取不到最新价")
        else:
            chosen.append(replace(spec, ref_price=prices[symbol]))
    if not han:
        skipped.append("中文合约名：合约清单里没有符合条件的")
    return chosen, skipped


async def fetch(timeout_s: float) -> tuple[dict[str, InstrumentSpec], dict[str, Decimal]]:
    feed = BinancePublicFeed(clock=_clock, timeout_s=timeout_s, retries=2, backoff_s=1.0)
    try:
        await feed.start()
        rows = await feed.call("fapiPublicGetTickerPrice", {})
    finally:
        await feed.stop()
    prices: dict[str, Decimal] = {}
    for row in rows if isinstance(rows, list) else []:
        try:
            price = to_decimal(row["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if price > 0:
            prices[str(row["symbol"])] = price
    return {i.symbol: i for i in feed.instruments()}, prices


def render(chosen: list[InstrumentSpec], generated_ms: int) -> str:
    data = {
        "generated_ms": generated_ms,
        "generated_utc": datetime.fromtimestamp(generated_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Binance USDⓈ-M 公共接口 GET /fapi/v1/exchangeInfo 与 GET /fapi/v1/ticker/price（ccxt 4.5.78，无密钥）",
        "note": "回放行情用的合约规则样例：ref_price 是生成时的最新价，回放把序列缩放到启动时刻等于它。"
                "由 dmr_standin/tools/make_sample.py 一次性生成，不要手改。",
        "instruments": [spec.to_sample_json() for spec in chosen],
    }
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dmr_standin.tools.make_sample",
                                     description="用 Binance 公共接口一次性生成回放用的合约规则样例")
    parser.add_argument("--out", type=Path, default=SAMPLE_PATH, help=f"输出文件（缺省 {SAMPLE_PATH}）")
    parser.add_argument("--timeout", type=float, default=15.0, help="每个请求的超时秒数")
    args = parser.parse_args(argv)
    instruments, prices = asyncio.run(fetch(args.timeout))
    chosen, skipped = choose(instruments, prices)
    if not chosen:
        print("一个合约也没选到，不写文件", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(chosen, _clock()), encoding="utf-8")
    print(f"写入 {args.out}：{len(chosen)} 个合约（合约清单共 {len(instruments)} 个）")
    for spec in chosen:
        print(f"  {spec.symbol}  tick={spec.to_json()['tick_size']}  step={spec.to_json()['step_size']}  "
              f"ref_price={spec.to_sample_json()['ref_price']}")
    for line in skipped:
        print(f"  跳过 {line}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
