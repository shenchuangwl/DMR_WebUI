"""命令行入口（docs/设计约定.md 3、7.1）::

    .venv-kernel/bin/python -m dmr_standin [--host 127.0.0.1] [--port 18220] [--state-dir var/standin]
        [--feed auto|binance|replay] [--equity 1000] [--no-persist] [--replay-dir <DMR_WebUI>/bit/data]
        [--log-dir <var>/log] [--selfcheck-feed]

- 只允许回环地址（127.0.0.0/8、::1、localhost）；其它地址直接拒绝（退出码 2）。
- 相对路径按 webui 根目录解析。首次启动在 state-dir 生成两把令牌（kernel.token、manual.token，0600）。
- 日志：标准库 logging，只在这里配置处理器（``<log-dir>/standin.log`` 按大小轮转 10MB×5，时间一律 UTC，同时写标准错误）；
  ``--log-dir`` 缺省是 ``<DMR_VAR_DIR 或 var>/log``，与内核、控制台放在一起。
- ``--no-persist``：不写状态快照与操作留痕（令牌照常生成，内核要读它）。
- ``--selfcheck-feed``：连一次 Binance 公共接口（合约清单、一根 K 线、一个最新价），打印结果后退出，不启动服务。
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import logging.handlers
import os
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

WEBUI_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 18220
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 5


def _path(text: str) -> Path:
    path = Path(text).expanduser()
    return path if path.is_absolute() else WEBUI_ROOT / path


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"端口必须是整数：{text!r}") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"端口必须在 0 到 65535 之间：{port}")
    return port


def default_log_dir() -> Path:
    var = os.environ.get("DMR_VAR_DIR", "").strip()
    return (_path(var) if var else WEBUI_ROOT / "var") / "log"


def _equity(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"初始资金必须是数值：{text!r}") from None
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError(f"初始资金必须为正：{text!r}")
    return value


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m dmr_standin",
                                     description="DMR 替身交易所（只用于开发与验收，不是产品功能）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址：只允许回环地址")
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT, help="监听端口（缺省 18220）")
    parser.add_argument("--state-dir", type=_path, default=WEBUI_ROOT / "var" / "standin",
                        help="令牌、状态快照、操作留痕所在目录（缺省 var/standin）")
    parser.add_argument("--feed", choices=("auto", "binance", "replay"), default="auto",
                        help="行情源：auto 先试 Binance 公共接口，不可用就整次运行用回放")
    parser.add_argument("--equity", type=_equity, default=Decimal("1000"), help="新账户的初始资金（USDT）")
    parser.add_argument("--no-persist", dest="persist", action="store_false", help="不写状态快照与操作留痕")
    parser.add_argument("--replay-dir", type=_path, default=WEBUI_ROOT.parent / "bit" / "data",
                        help="回放用的 DMR CSV 目录（只读）")
    parser.add_argument("--log-dir", type=_path, default=default_log_dir(),
                        help="日志目录（缺省 <DMR_VAR_DIR 或 var>/log）；standin.log 按 10MB×5 轮转，时间为 UTC")
    parser.add_argument("--selfcheck-feed", action="store_true",
                        help="连一次 Binance 公共接口，打印合约数量、一根 K 线、一个最新价后退出")
    args = parser.parse_args(argv)
    if not is_loopback(args.host):
        parser.error(f"--host 只允许回环地址（127.0.0.0/8、::1、localhost），拒绝 {args.host!r}")
    return args


async def _selfcheck() -> int:
    from .dispatcher import system_clock
    from .feed import BinancePublicFeed, FeedError

    feed = BinancePublicFeed(clock=system_clock, timeout_s=10.0, retries=1)
    try:
        await feed.start()
        instruments = feed.instruments()
        tradable = [i for i in instruments if i.contract_type == "PERPETUAL" and i.quote == "USDT"
                    and i.status == "TRADING"]
        print(f"合约清单：{len(instruments)} 个（其中 USDT 永续、交易中 {len(tradable)} 个）")
        bar = (await feed.klines("BTCUSDT", "15m", 1))[0]
        print(f"BTCUSDT 15m 最新一根 K 线：{bar}")
        await feed.ensure_quotes(["BTCUSDT"])
        quote = feed.quote("BTCUSDT")
        print(f"BTCUSDT 最新价 {quote.last}，标记价 {quote.mark}，资金费率 {feed.funding_rate('BTCUSDT')}")
        return 0
    except (FeedError, PermissionError) as exc:
        print(f"自检失败：{exc}", file=sys.stderr)
        return 1
    finally:
        await feed.stop()


class _DropNoiseFilter(logging.Filter):
    """drop 故障故意不给完整响应，uvicorn 会为此报错；替身自己另记一条说明，这里把这条报错滤掉。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return "returned without completing response" not in record.getMessage()


def configure_logging(log_dir: Path) -> None:
    """只在这里配置处理器（约定第 6 节）：标准错误 + ``<log_dir>/standin.log`` 按大小轮转，时间一律 UTC。重复调用不会叠加处理器。"""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_dmr_standin", False)]:
        root.removeHandler(handler)
        handler.close()
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    for handler in (
        logging.handlers.RotatingFileHandler(log_dir / "standin.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                                             encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ):
        handler.setFormatter(formatter)
        handler._dmr_standin = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def _url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


async def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .dispatcher import build_standin, system_clock
    from .feed import open_feed
    from .persistence import ensure_tokens
    from .server import create_app

    kernel_token, manual_token = ensure_tokens(args.state_dir)
    feed = await open_feed(args.feed, clock=system_clock, replay_dir=args.replay_dir)
    try:
        disp = build_standin(feed=feed, clock=system_clock, equity=args.equity, kernel_token=kernel_token,
                             manual_token=manual_token, state_dir=args.state_dir, persist=args.persist)
        status = feed.status()
        print(f"DMR 替身交易所：{_url(args.host, args.port)}（行情源 {status['source']}，"
              f"状态目录 {args.state_dir}，{'持久化' if args.persist else '不持久化'}）", flush=True)
        for err in status["errors"]:
            print(f"  行情源：{err['msg']}", flush=True)
        configure_logging(args.log_dir)
        logging.getLogger("uvicorn.error").addFilter(_DropNoiseFilter())
        config = uvicorn.Config(create_app(disp, background=True), host=args.host, port=args.port,
                                log_level="info", log_config=None, access_log=False, lifespan="on")
        await uvicorn.Server(config).serve()
    finally:
        await feed.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.selfcheck_feed:
        return asyncio.run(_selfcheck())
    from .feed import FeedError

    try:
        return asyncio.run(_serve(args))
    except (FeedError, ValueError, OSError) as exc:  # 行情源不可用（--feed binance）、快照或令牌损坏、端口被占用
        print(f"替身交易所启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
