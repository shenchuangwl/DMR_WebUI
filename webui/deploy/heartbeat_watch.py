"""内核心跳看护（由 dmr-heartbeat.timer 每 30 秒运行一次，用内核环境）。

读内核心跳文件（缺省 ``<DMR_VAR_DIR>/kernel/heartbeat.json``，由 ``dmr_kernel.alerts.heartbeat.Heartbeat`` 写），超过
``--alert-after-s``（缺省 60）秒没更新、或文件缺失/损坏，就退出 1 并推送告警：停止超过 60 秒即推送，加上定时器的 30 秒间隔，
90 秒内应当收到（报告 4.13）。

- 只读一个环境变量 ``DMR_ALERT_URLS``（Apprise 地址，逗号分隔；里面带令牌，永远不打印、不写进告警正文）。没配地址时只在标准错误报告。
- 推送限速：成功送达后记在 ``<DMR_VAR_DIR>/run/heartbeat_alert.json``，``--repeat-s``（缺省 900）秒内不重复推送，
  免得内核停一晚上就收到上千条；心跳恢复后清除，下次再停立即告警。没送达就不记，下次继续试。
- 退出码：0 心跳正常（不输出任何东西）；1 心跳停止或缺失；2 参数错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

WEBUI_ROOT = Path(__file__).resolve().parents[1]
if str(WEBUI_ROOT) not in sys.path:
    sys.path.insert(0, str(WEBUI_ROOT))

from dmr_kernel.alerts.heartbeat import Heartbeat  # noqa: E402
from dmr_kernel.core.types import AlertLevel  # noqa: E402

DEFAULT_ALERT_AFTER_S = 60
DEFAULT_REPEAT_S = 900
TITLE = "DMR 内核心跳停止"

Sender = Callable[[str, str, str, AlertLevel], bool]  # (渠道地址, 标题, 正文, 级别) -> 是否送达


def _due(state_path: Path, now_ms: int, repeat_s: int) -> bool:
    try:
        last = json.loads(state_path.read_text(encoding="utf-8"))["last_alert_ms"]
    except (OSError, ValueError, KeyError, TypeError):
        return True
    if isinstance(last, bool) or not isinstance(last, int):
        return True
    elapsed_ms = now_ms - last
    return not 0 <= elapsed_ms < repeat_s * 1000  # 时钟回拨（elapsed < 0）时也算到期，不会永远不告警


def _write_state(state_path: Path, now_ms: int) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"last_alert_ms": now_ms}), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, state_path)


def _clear_state(state_path: Path) -> None:
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass


def run(path: Path, *, now_ms: int, alert_after_s: int, urls: Sequence[str], sender: Sender | None, state_path: Path,
        repeat_s: int, out: TextIO) -> int:
    """检查一次。返回退出码（0 正常，1 心跳停止或缺失）。地址与异常内容都不输出，只记异常类型。"""
    stale, age_s = Heartbeat.stale(path, now_ms, alert_after_s)
    if not stale:
        _clear_state(state_path)
        return 0
    if age_s is None:
        message = f"没有可用的内核心跳（{path} 不存在或内容损坏）"
    else:
        message = f"内核心跳已停止 {age_s:.0f} 秒（超过 {alert_after_s} 秒）"
    print(message, file=out)
    if urls and sender is not None and _due(state_path, now_ms, repeat_s):
        delivered = 0
        for url in urls:
            try:
                if sender(url, TITLE, message, AlertLevel.CRITICAL):
                    delivered += 1
            except Exception as exc:  # noqa: BLE001 - 一个渠道出错不能影响别的渠道，也不能让看护本身崩掉
                print(f"推送失败：{type(exc).__name__}", file=out)
        if delivered:
            _write_state(state_path, now_ms)
        else:
            print("告警没有送达任何渠道，下次运行再试", file=out)
    return 1


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"必须是整数：{text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"必须 ≥ 1：{value}")
    return value


def _non_negative(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"必须是整数：{text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"必须 ≥ 0：{value}")
    return value


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="heartbeat_watch.py", description="内核心跳看护：心跳停止超过阈值就退出 1 并推送告警")
    parser.add_argument("--path", type=Path, default=None, help="心跳文件（缺省 <DMR_VAR_DIR>/kernel/heartbeat.json）")
    parser.add_argument("--alert-after-s", type=_positive, default=DEFAULT_ALERT_AFTER_S,
                        help="心跳停止多少秒算异常（缺省 60）")
    parser.add_argument("--repeat-s", type=_non_negative, default=DEFAULT_REPEAT_S,
                        help="持续异常时两次推送的最短间隔（缺省 900 秒）")
    parser.add_argument("--state", type=Path, default=None, help="记录上次推送时间的文件（缺省 <DMR_VAR_DIR>/run/heartbeat_alert.json）")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None, *, now_ms: int | None = None,
         sender: Sender | None = None, out: TextIO | None = None) -> int:
    env = os.environ if env is None else env
    args = parse_args(argv)
    var_dir = Path(env.get("DMR_VAR_DIR") or WEBUI_ROOT / "var")
    urls = tuple(part.strip() for part in env.get("DMR_ALERT_URLS", "").split(",") if part.strip())
    if sender is None and urls:
        from dmr_kernel.alerts.notifier import apprise_send  # 延迟导入：没配地址时不加载 Apprise

        sender = apprise_send
    return run(args.path or var_dir / "kernel" / "heartbeat.json",
               now_ms=int(time.time() * 1000) if now_ms is None else now_ms, alert_after_s=args.alert_after_s,
               urls=urls, sender=sender, state_path=args.state or var_dir / "run" / "heartbeat_alert.json",
               repeat_s=args.repeat_s, out=sys.stderr if out is None else out)


if __name__ == "__main__":
    sys.exit(main())
