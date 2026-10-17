"""命令行客户端测试的公共帮手：假的 HTTP 传输（记下每个请求、按顺序给出预设的答复或异常）、
带临时 var 目录的运行台（环境变量、本机令牌文件、捕获输出）。命令行不碰数据库、不连网：全程不开任何套接字。

放在单独的模块里而不是 conftest.py：各目录的 conftest 同名，互相 import 会串。"""
from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timezone
from io import StringIO
from pathlib import Path
from typing import Any

from dmr_kernel.cli.client import HttpResponse, KernelUnreachable
from dmr_kernel.cli.main import main


def json_response(status: int, payload: Any) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def error_response(status: int, code: str, message: str) -> HttpResponse:
    return json_response(status, {"code": code, "message": message})


@dataclass
class Sent:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes | None

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


@dataclass
class FakeTransport:
    """按顺序给出预设的答复；预设项可以是答复、异常（会被抛出）或可调用对象（收到 Sent，返回前两者之一）。
    预设用完后再来请求就报错——不让测试悄悄多发请求。"""

    script: list[Any] = field(default_factory=list)
    sent: list[Sent] = field(default_factory=list)

    def request(self, method: str, path: str, *, headers: Mapping[str, str], body: bytes | None = None) -> HttpResponse:
        record = Sent(method, path, dict(headers), body)
        self.sent.append(record)
        if not self.script:
            raise AssertionError(f"没有预设的答复了，却又来了一个请求：{method} {path}")
        step = self.script.pop(0)
        if callable(step):
            step = step(record)
        if isinstance(step, BaseException):
            raise step
        return step


def transport(*steps: Any) -> FakeTransport:
    return FakeTransport(list(steps))


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err


class Rig:
    """一个临时的 var 目录 + 本机令牌 + 运行命令行的入口。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.var = tmp_path / "var"
        self.env = {"DMR_VAR_DIR": str(self.var)}
        self.token = secrets.token_urlsafe(32)
        self.token_path = self.var / "kernel" / "local.token"
        self.settings_seen: list[Any] = []

    def write_token(self, token: str | None = None, *, raw: str | None = None) -> Path:
        self.token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.token_path.write_text(raw if raw is not None else (token or self.token) + "\n", encoding="utf-8")
        os.chmod(self.token_path, 0o600)
        return self.token_path

    def run(self, argv: list[str], fake: FakeTransport | None = None, *, with_token: bool = True,
            env: Mapping[str, str] | None = None) -> Result:
        if with_token and not self.token_path.exists():
            self.write_token()
        out, err = StringIO(), StringIO()

        def factory(settings):
            self.settings_seen.append(settings)
            return fake if fake is not None else FakeTransport()

        code = main(argv, env={**self.env, **(env or {})}, transport_factory=factory, stdout=out, stderr=err,
                    tz=timezone.utc)
        return Result(code, out.getvalue(), err.getvalue())

    # ------------------------------------------------------------ 文件系统的观察

    @property
    def operators_dir(self) -> Path:
        return self.var / "operators"

    def password_path(self, username: str) -> Path:
        return self.operators_dir / f"{username}.password"

    @staticmethod
    def mode_of(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)


UNREACHABLE: Callable[[str], KernelUnreachable] = lambda kind: KernelUnreachable(kind, "连接被拒绝")  # noqa: E731
