"""命令行的两个命令（第 7 层 cli）：``status``、``create-operator``。只经控制接口说话：不打开数据库，除本机令牌文件和口令文件外不读写任何文件。

- ``status``：``GET /state``，把要点用中文打印出来。
- ``create-operator``：随机生成口令 → **先**以 0600 独占创建口令文件 → 凭本机令牌 ``POST /operators`` → 失败就删掉口令文件。
  终端上只打印口令文件的路径，**绝不打印口令**（令牌同样不打印）。

  * 先落盘、后创建：万一内核已经建好、答复却丢了，口令也不会丢。
  * 请求肯定没到内核（连不上、内核拒绝了、本地出错）→ 删掉口令文件；结果不确定（请求发出后没拿到完整的答复，或被 Ctrl-C 打断）
    → 保留口令文件，让操作者用它登录试一试：误删会让一个已存在的用户名永远没人知道口令，而多留一个文件随时可以删。
  * 口令文件从不覆盖、不跟随符号链接（``O_EXCL | O_NOFOLLOW``）。
"""
from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from typing import TextIO

from dmr_kernel.cli.client import KernelClient, KernelError, KernelUnreachable
from dmr_kernel.cli.render import format_state
from dmr_kernel.settings import Settings

__all__ = [
    "EMERGENCY_HINT", "EXIT_FAILED", "EXIT_INTERRUPTED", "EXIT_OK", "EXIT_UNAVAILABLE", "EXIT_USAGE", "USERNAME_RE",
    "USERNAME_RULE", "CommandContext", "TokenFileError", "default_password_path", "new_password", "read_local_token",
    "run_create_operator", "run_status",
]

EXIT_OK = 0
EXIT_FAILED = 1  # 内核答复了错误，或本地文件有问题
EXIT_USAGE = 2  # 参数或启动配置不对（与 argparse 的用法错误一致）
EXIT_UNAVAILABLE = 3  # 连不上内核，或没拿到完整的答复
EXIT_INTERRUPTED = 130  # Ctrl-C

# 与内核（api.auth）的规则一致：测试对拍。命令行不导入内核的包，所以这里各写一份
USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")
USERNAME_RULE = "用户名只能是 1 到 32 位的字母、数字、下划线、点或连字符"
PASSWORD_BYTES = 32  # secrets.token_urlsafe(32)：43 个 URL 安全字符，256 位随机；内核要求 8 到 1024 个字符
FILE_MODE = 0o600
DIR_MODE = 0o700
_TOKEN_RE = re.compile(r"[\x21-\x7e]+")  # 令牌是一行可见的 ASCII（内核写的是 token_urlsafe）；别的内容放进请求头会出错

EMERGENCY_HINT = ("内核不可用时的命令行应急入口（T1.7）：python -m dmr_kernel.cli emergency close"
                  "（停内核、逐仓确认后平掉全账户持仓）；也可以直接在 Binance 处置。")
UNCERTAIN_NEXT = "请先用该文件里的口令登录控制台试一试：能登录就不用再做什么；登录不了说明没有创建成功，删掉该文件后重新运行本命令。"


@dataclass(frozen=True)
class CommandContext:
    settings: Settings
    client: KernelClient
    out: TextIO
    err: TextIO
    tz: tzinfo | None = None
    ask: Callable[[str], str] = input  # 逐个确认用；输入结束（EOFError）按「否」处理
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


class TokenFileError(Exception):
    """本机令牌文件有问题。``code`` 是命令行的退出码，``message`` 是给操作者看的中文说明（不含令牌）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def read_local_token(path: Path) -> str:
    """读本机令牌（``var/kernel/local.token``，内核每次启动重写）。没有这个文件说明内核从没在这里启动过。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TokenFileError(EXIT_UNAVAILABLE, f"找不到本机令牌文件：{path}\n内核还没有在这个运行目录下启动过"
                                               "（内核启动时会写出这个文件）。请先启动内核。") from None
    except PermissionError:
        raise TokenFileError(EXIT_FAILED, f"无权读取本机令牌文件：{path}\n请用启动内核的那个系统用户运行本命令。") from None
    except IsADirectoryError:
        raise TokenFileError(EXIT_FAILED, f"本机令牌文件的位置上是一个目录：{path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        raise TokenFileError(EXIT_FAILED, f"无法读取本机令牌文件：{path}（{reason}）") from None
    token = text.strip()
    if not token:
        raise TokenFileError(EXIT_FAILED, f"本机令牌文件是空的：{path}\n内核可能没有启动完成，请稍后重试；仍不行就重启内核。")
    if not _TOKEN_RE.fullmatch(token):
        raise TokenFileError(EXIT_FAILED, f"本机令牌文件的内容不是合法的令牌（应是一行可见的 ASCII 字符）：{path}\n"
                                          "请重启内核让它重写这个文件。")
    return token


def new_password() -> str:
    return secrets.token_urlsafe(PASSWORD_BYTES)


def default_password_path(settings: Settings, username: str) -> Path:
    return settings.var_dir / "operators" / f"{username}.password"


# ---------------------------------------------------------------- 内核的答复 -> 中文说明


def _unreachable_lines(settings: Settings, exc: KernelUnreachable) -> list[str]:
    where = f"{settings.kernel_host}:{settings.kernel_port}"
    if exc.kind == "connect":
        return [f"无法连接内核（{where}）：{exc.detail}。",
                "内核可能没有运行，或这里的地址与内核的 DMR_KERNEL_HOST、DMR_KERNEL_PORT 设置不一致。",
                EMERGENCY_HINT]
    return [f"内核（{where}）没有给出完整的答复：{exc.detail}。", EMERGENCY_HINT]


def _refusal_lines(settings: Settings, exc: KernelError) -> list[str]:
    if exc.code == "unknown":  # 认不出的报文：message 已经是「内核答复了 HTTP nnn」
        lines = [exc.message]
    else:
        lines = [f"内核拒绝了请求（HTTP {exc.status}，{exc.code}）：{exc.message}"]
    if exc.status == 401:
        lines.append(f"内核不认本机令牌（{settings.local_token_path}）。本机令牌在内核每次启动时重新生成：请确认内核是用同一个"
                     "运行目录启动的；内核刚重启过的话，重新运行本命令即可。")
    return lines


def _say(stream: TextIO, lines: list[str]) -> None:
    for line in lines:
        print(line, file=stream)


# ---------------------------------------------------------------- status


def run_status(ctx: CommandContext) -> int:
    try:
        state = ctx.client.get("/state")
    except KernelUnreachable as exc:
        _say(ctx.err, _unreachable_lines(ctx.settings, exc))
        return EXIT_UNAVAILABLE
    except KernelError as exc:
        _say(ctx.err, _refusal_lines(ctx.settings, exc))
        return EXIT_FAILED
    _say(ctx.out, format_state(state, tz=ctx.tz))
    return EXIT_OK


# ---------------------------------------------------------------- create-operator


def _make_private_dirs(directory: Path) -> None:
    """建出缺的目录（0700，不受 umask 影响）；已有的目录一概不动。"""
    missing: list[Path] = []
    current = directory
    while not current.exists():  # 悬空的符号链接也算不存在：下面 mkdir 会失败，当作无法创建
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for item in reversed(missing):
        item.mkdir(mode=DIR_MODE)
        item.chmod(DIR_MODE)  # mkdir 的 mode 要过 umask：严格的 umask 会把它削成不可写


def _create_password_file(path: Path, password: str) -> None:
    """独占创建（已有任何东西——文件、目录、符号链接，哪怕悬空——都失败，不覆盖、不跟随）并写入口令，落盘后返回。
    中途出错就删掉半成品再抛。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            os.fchmod(handle.fileno(), FILE_MODE)  # 创建时的 mode 要过 umask：严格的 umask 会削掉
            handle.write(password + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with suppress(OSError):
            os.unlink(path)
        raise


def _discard(path: Path) -> str:
    """删掉口令文件；返回一句话，说明删掉了没有。"""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return "口令文件已删除。"
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        return f"无法删除口令文件 {path}（{reason}）：里面的口令没有对应任何操作员，请手动删除它。"
    return "口令文件已删除。"


def _discard_quietly(path: Path) -> None:
    with suppress(OSError):
        os.unlink(path)


def run_create_operator(ctx: CommandContext, username: str, password_file: Path | None) -> int:
    """``username`` 已由调用方校验过（``USERNAME_RE``）。``password_file`` 缺省时用 ``var/operators/<名>.password``。"""
    custom = password_file is not None
    # 终端上打印的路径一律是绝对路径：操作者可以原样拿去用，不依赖当前所在目录（不解析符号链接，只规整 ..）
    path = Path(os.path.abspath(password_file if custom else default_password_path(ctx.settings, username)))
    password = new_password()
    try:
        _make_private_dirs(path.parent)
        if not custom:
            path.parent.chmod(DIR_MODE)  # 默认目录是我们的：以前建得松了就收紧；别人的目录（自选路径）不动
        _create_password_file(path, password)
    except FileExistsError:
        _say(ctx.err, [f"口令文件已存在：{path}", "不会覆盖它：如果确认不再需要，请先自行删除；或者用 --password-file 另指一个路径。"])
        return EXIT_FAILED
    except OSError as exc:
        _say(ctx.err, [f"无法创建口令文件：{path}（{exc.strerror or type(exc).__name__}）", "没有向内核发送任何请求。"])
        return EXIT_FAILED

    try:
        ctx.client.post("/operators", {"username": username, "password": password})
    except KernelError as exc:
        lines = _refusal_lines(ctx.settings, exc)
        _say(ctx.err, ["创建失败：" + lines[0], *lines[1:], _discard(path)])
        return EXIT_FAILED
    except KernelUnreachable as exc:
        if exc.kind == "connect":  # 请求肯定没到内核
            _say(ctx.err, [*_unreachable_lines(ctx.settings, exc), _discard(path)])
        else:  # 请求可能已被处理：口令文件留着，口令不能丢
            _say(ctx.err, [*_unreachable_lines(ctx.settings, exc),
                           f"不确定内核是否已经创建了操作员 {username}。口令文件已保留：{path}", UNCERTAIN_NEXT])
        return EXIT_UNAVAILABLE
    except KeyboardInterrupt:
        _say(ctx.err, [f"已中断。请求可能已经送达内核，不确定操作员 {username} 是否已创建。口令文件已保留：{path}", UNCERTAIN_NEXT])
        return EXIT_INTERRUPTED
    except BaseException:  # 没预料到的错误：请求大概率根本没发出去，别留下没人用的口令
        _discard_quietly(path)
        raise
    _say(ctx.out, [f"操作员 {username} 已创建。", f"口令文件：{path}（权限 0600；口令在其中单独一行）",
                   "请取出口令妥善保管：内核只保存口令的哈希，口令丢了无法找回。"])
    return EXIT_OK
