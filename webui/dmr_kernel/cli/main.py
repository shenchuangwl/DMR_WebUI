"""命令行入口（第 7 层 cli）：``python -m dmr_kernel.cli status | create-operator | stop | close-all | inventory | emergency close``。

除 ``emergency close``（内核不可用时的应急路径，见 ``cli/emergency``）外，只经控制接口（``127.0.0.1:18200``）与内核说话，不打开数据库，不依赖任何第三方库。内核地址与运行目录取 ``dmr_kernel.settings``
（环境变量 ``DMR_KERNEL_HOST``、``DMR_KERNEL_PORT``、``DMR_VAR_DIR``），本机令牌取 ``<运行目录>/kernel/local.token``。

退出码：0 成功；1 内核拒绝了请求或本地文件有问题；2 用法或启动配置不对；3 连不上内核或没拿到完整的答复；130 被 Ctrl-C 打断。
``main`` 的 ``env``、``transport_factory``、``stdout``、``stderr``、``tz``、``ask``、``sleep``、``monotonic``、``emergency_deps`` 都可注入（测试用假传输、不开套接字）；
argparse 自己的用法与帮助输出照常走进程的标准输出和标准错误。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import tzinfo
from pathlib import Path
from typing import NoReturn, TextIO

from dmr_kernel.cli.client import HttpTransport, KernelClient, Transport
from dmr_kernel.cli.commands import (
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    USERNAME_RE,
    USERNAME_RULE,
    CommandContext,
    TokenFileError,
    read_local_token,
    run_create_operator,
    run_status,
)
from dmr_kernel.cli.emergency import EmergencyDeps, run_emergency_close
from dmr_kernel.cli.inventory import run_inventory
from dmr_kernel.cli.ops_commands import run_close_all, run_stop
from dmr_kernel.settings import Settings, SettingsError, load_settings

__all__ = ["EXIT_FAILED", "EXIT_INTERRUPTED", "EXIT_OK", "EXIT_UNAVAILABLE", "EXIT_USAGE", "WEBUI_ROOT", "build_parser",
           "main"]

WEBUI_ROOT = Path(__file__).resolve().parents[2]
PROG = "python -m dmr_kernel.cli"
MAX_SHOWN = 40  # 回显用户输入时最多显示多少个字符


class _Parser(argparse.ArgumentParser):
    """用法错误：中文前缀 + argparse 的原话，退出码 2。"""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        hint = ""
        if "--password" in message and "--password-file" not in message:
            hint = "\n口令由 create-operator 随机生成并写进口令文件，不能在命令行里指定。"
        self.exit(EXIT_USAGE, f"{self.prog}：用法错误：{message}{hint}\n运行 {PROG} --help 查看用法。\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG, allow_abbrev=False,
        description="DMR 内核命令行：只经控制接口（127.0.0.1:18200）与内核通信，不打开数据库。",
        epilog="内核地址与运行目录取环境变量 DMR_KERNEL_HOST、DMR_KERNEL_PORT、DMR_VAR_DIR（缺省 127.0.0.1:18200、webui/var）；"
               "本机令牌在 <运行目录>/kernel/local.token。")
    commands = parser.add_subparsers(dest="command", parser_class=_Parser, required=True,
                                     metavar="{status,create-operator,stop,close-all,inventory,emergency}")
    commands.add_parser("status", allow_abbrev=False, help="查看内核状态要点（中文）",
                        description="凭本机令牌调用 GET /api/v1/state，用中文打印内核状态的要点。")
    create = commands.add_parser(
        "create-operator", allow_abbrev=False, help="创建操作员（口令随机生成，写进 0600 的口令文件，终端不显示口令）",
        description="随机生成口令，先以 0600 独占创建口令文件，再凭本机令牌调用 POST /api/v1/operators 创建操作员；"
                    "失败则删掉口令文件。终端上只打印口令文件的路径，绝不打印口令。")
    create.add_argument("--username", required=True, metavar="名", help=USERNAME_RULE)
    create.add_argument("--password-file", metavar="路径", type=Path,
                        help="口令文件的路径（缺省 <运行目录>/operators/<名>.password；目录不存在时建成 0700；已有同名文件时不覆盖）")
    commands.add_parser("stop", allow_abbrev=False, help="停止开仓（内核存活时；凭本机令牌，不占席位）",
                        description="调用 POST /ops/stop 关闸：不再产生开仓意图，撤未成交的开仓单；已有仓的止损照常更新。")
    close_all = commands.add_parser(
        "close-all", allow_abbrev=False, help="全部托管持仓市价平仓（内核存活时；范围固定为全部托管持仓）",
        description="预览 POST /ops/close_all/preview，确认后 POST /ops/close_all/confirm，并轮询 /operations/{op_id} 逐仓打印结果。"
                    "范围固定为全部托管持仓（不含待确认与外部的仓）。")
    close_all.add_argument("--yes", action="store_true", help="不再逐次确认（脚本用）")
    commands.add_parser("inventory", allow_abbrev=False, help="清点全部持仓与挂单（含外部、待确认）并提示可选动作（只读）",
                        description="只读：列出全部持仓与挂单，给出每一项可选的动作（平掉、接管、忽略）；本命令不执行任何动作。")
    emergency = commands.add_parser(
        "emergency", allow_abbrev=False, help="内核不可用时的应急入口",
        description="内核不可用（失联、卡死）时的应急路径：不经控制接口，直接对替身/交易所逐仓平仓。")
    emergency_commands = emergency.add_subparsers(dest="emergency_command", metavar="{close}", parser_class=_Parser,
                                                  required=True)
    close = emergency_commands.add_parser(
        "close", allow_abbrev=False, help="停内核、取内核锁、逐仓确认后平掉全账户持仓",
        description="写停用标记 var/run/kernel.nosupervise；按 var/run/kernel.pid 停内核（SIGTERM，卡死则 SIGKILL）；取 var/kernel.lock；"
                    "读全账户持仓逐仓列出，逐个确认后发市价平仓单，归零后撤该合约该方向上带系统前缀的条件单；不打开数据库。"
                    "结束后停用标记仍在，确认后手动删除再启动内核。")
    close.add_argument("--yes", action="store_true", help="不逐个确认（只给测试用；真实环境请逐个确认）")
    return parser


def _default_transport(settings: Settings) -> Transport:
    return HttpTransport(settings.kernel_host, settings.kernel_port)


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         transport_factory: Callable[[Settings], Transport] | None = None, stdout: TextIO | None = None,
         stderr: TextIO | None = None, tz: tzinfo | None = None, ask: Callable[[str], str] | None = None,
         sleep: Callable[[float], None] | None = None, monotonic: Callable[[], float] | None = None,
         emergency_deps: EmergencyDeps | None = None) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as stop:  # 用法错误（2）与 --help（0）：argparse 已经打印过了
        return stop.code if isinstance(stop.code, int) else EXIT_USAGE
    if args.command == "create-operator" and not USERNAME_RE.fullmatch(args.username):
        print(f"{USERNAME_RULE}：{args.username[:MAX_SHOWN]!r}", file=err)
        return EXIT_USAGE
    try:
        settings = load_settings(os.environ if env is None else env, webui_root=WEBUI_ROOT)
    except SettingsError as exc:
        print(f"启动配置错误：{exc}", file=err)
        return EXIT_USAGE
    if args.command == "emergency":  # 内核不可用时用：不需要本机令牌，也不连控制接口
        deps = emergency_deps or EmergencyDeps()
        deps.ask = deps.ask or ask
        deps.sleep = deps.sleep or sleep
        deps.monotonic = deps.monotonic or monotonic
        try:
            return run_emergency_close(settings, yes=args.yes, out=out, err=err, deps=deps)
        except KeyboardInterrupt:
            print("已中断。", file=err)
            return EXIT_INTERRUPTED
    try:
        token = read_local_token(settings.local_token_path)  # 没有令牌就不去连：内核从没在这里启动过
    except TokenFileError as exc:
        print(exc.message, file=err)
        return exc.code
    client = KernelClient((transport_factory or _default_transport)(settings), token)
    ctx = CommandContext(settings, client, out, err, tz, ask or input, sleep or time.sleep, monotonic or time.monotonic)
    try:
        if args.command == "status":
            return run_status(ctx)
        if args.command == "stop":
            return run_stop(ctx)
        if args.command == "close-all":
            return run_close_all(ctx, yes=args.yes)
        if args.command == "inventory":
            return run_inventory(ctx)
        return run_create_operator(ctx, args.username, args.password_file)
    except KeyboardInterrupt:
        print("已中断。", file=err)
        return EXIT_INTERRUPTED
